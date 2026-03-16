import camera
import time
import dht
import machine
import network
import socket
import os
import gc
from machine import Pin, ADC

# ====================== 配置区（根据你的情况修改）======================
# 你家的WiFi配置（2.4G WiFi）
WIFI_SSID = "名字"
WIFI_PASSWORD = "密码"
# 自动循环采集间隔（单位：秒）
AUTO_COLLECT_INTERVAL = 10
# 【已校准】你的传感器参数
DRY_VALUE = 2933
WET_VALUE = 65
# ====================== 电脑AI识别页面地址（改成你电脑的IP）======================
AI_PAGE_URL = "电脑AI识别页面地址"

# ====================== 【修复内存问题】摄像头配置 ======================
# 拍照：SVGA分辨率，AI识别足够清晰，不会爆内存
PHOTO_FRAMESIZE = camera.FRAME_SVGA    # 800x600 高清，平衡清晰度和内存
PHOTO_QUALITY = 10                      # 画质适中，兼顾细节和体积
# 准实时画面保持不变，不影响流畅度
LIVE_FRAMESIZE = camera.FRAME_QCIF
LIVE_QUALITY = 30
LIVE_REFRESH_INTERVAL = 3000

# ====================== 全局状态变量 ======================
auto_collect_enabled = False
last_collect_time = 0
wifi_connected = False
device_ip = "0.0.0.0"
latest_live_frame = None
latest_photo = None

# ====================== 硬件初始化 ======================
dht_sensor = dht.DHT11(machine.Pin(2))
soil_sensor = ADC(Pin(1))
soil_sensor.atten(ADC.ATTN_11DB)
soil_sensor.width(ADC.WIDTH_12BIT)
# 开启内存自动回收，避免内存泄漏
gc.enable()
# 强制回收一次初始内存
gc.collect()

# ====================== 工具函数：检查Flash剩余空间 ======================
def check_flash_free():
    """检查Flash剩余空间，避免存满导致拍照失败"""
    stat = os.statvfs('/')
    free_space = stat[0] * stat[3]
    return free_space  # 单位：字节

# ====================== 双模式摄像头 ======================
def switch_camera_mode(mode):
    """切换摄像头模式：'live'准实时，'photo'拍照高清"""
    try:
        camera.deinit()
        gc.collect() # 切换前强制回收内存
        time.sleep_ms(100)
        if mode == "live":
            camera.init(
                0,
                format=camera.JPEG,
                framesize=LIVE_FRAMESIZE,
                fb_location=camera.PSRAM,
                quality=LIVE_QUALITY
            )
        else:
            camera.init(
                0,
                format=camera.JPEG,
                framesize=PHOTO_FRAMESIZE,
                fb_location=camera.PSRAM,
                quality=PHOTO_QUALITY
            )
        time.sleep_ms(100)
        gc.collect()
        return True
    except Exception as e:
        print(f"摄像头切换失败: {e}")
        return False

# ====================== 功能函数 ======================
def read_dht11_safe():
    for _ in range(2):
        try:
            dht_sensor.measure()
            temp, humi = dht_sensor.temperature(), dht_sensor.humidity()
            if 0 <= temp <= 50 and 20 <= humi <= 90:
                return temp, humi
            time.sleep_ms(30)
        except:
            time.sleep_ms(30)
    return None, None

def read_soil_safe():
    val = soil_sensor.read()
    if 0 <= val <= 4095:
        if val >= DRY_VALUE:
            moisture = 0.0
        elif val <= WET_VALUE:
            moisture = 100.0
        else:
            moisture = (DRY_VALUE - val) / (DRY_VALUE - WET_VALUE) * 100
        moisture = round(moisture, 2)
        return val, moisture
    return None, None

def save_to_csv(timestamp, temp, humi, soil_raw, soil_moist, photo_name):
    try:
        csv_file = "data_log.csv"
        exists = csv_file in os.listdir()
        with open(csv_file, "a", encoding="utf-8") as f:
            if not exists:
                f.write("时间戳,温度(℃),湿度(%RH),土壤原始值,土壤湿度(%),照片文件名\n")
            f.write(f"{timestamp},{temp},{humi},{soil_raw},{soil_moist},{photo_name}\n")
    except:
        pass

# ====================== 【修复】拍照函数，解决内存不足问题 ======================
def take_photo_ai():
    try:
        # 先检查Flash剩余空间，低于500KB就不拍照了
        free_space = check_flash_free()
        if free_space < 500 * 1024:
            raise Exception(f"Flash空间不足，剩余{free_space/1024:.1f}KB，请删除旧照片")
        
        # 切换拍照模式
        if not switch_camera_mode("photo"):
            return None, None
        
        # 读取传感器数据
        temp, humi = read_dht11_safe()
        soil_raw, soil_moist = read_soil_safe()
        timestamp = int(time.time())
        
        # 拍照
        img_buf = camera.capture()
        if not img_buf or len(img_buf) == 0:
            raise Exception("拍照无数据")
        
        # 生成文件名
        if temp is not None and humi is not None and soil_moist is not None:
            filename = f"photo_{timestamp}_temp{temp}_hum{humi}_soil{soil_moist}.jpg"
        else:
            filename = f"photo_{timestamp}.jpg"
        
        # 保存照片
        with open(filename, "wb") as f:
            f.write(img_buf)
        
        # 保存CSV日志
        if temp and humi and soil_raw:
            save_to_csv(timestamp, temp, humi, soil_raw, soil_moist, filename)
        
        print(f"✅ AI高清拍照完成：{filename}，大小：{len(img_buf)/1024:.1f}KB，Flash剩余：{free_space/1024:.1f}KB")
        
        # 切回准实时模式
        switch_camera_mode("live")
        # 强制回收内存
        del img_buf
        gc.collect()
        return filename, None
    except Exception as e:
        print(f"拍照失败: {e}")
        # 出错也强制切回准实时模式
        try:
            switch_camera_mode("live")
        except:
            pass
        gc.collect()
        return None, None

# ====================== 准实时画面更新 ======================
def update_live_frame():
    """后台更新准实时画面，不阻塞主线程"""
    global latest_live_frame
    try:
        buf = camera.capture()
        if buf:
            latest_live_frame = buf
    except:
        pass

# ====================== WiFi连接函数 ======================
def connect_wifi():
    global wifi_connected, device_ip
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.disconnect()
    time.sleep(0.5)

    print(f"正在连接WiFi: {WIFI_SSID}")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    
    retry = 0
    while not wlan.isconnected() and retry < 20:
        time.sleep(0.5)
        retry += 1
        print(f"连接中... {retry}/20")

    if wlan.isconnected():
        wifi_connected = True
        device_ip = wlan.ifconfig()[0]
        print("\n" + "="*50)
        print(f"✅ WiFi连接成功！")
        print(f"👉 请在浏览器打开：http://{device_ip}")
        print("="*50 + "\n")
        return True
    else:
        wifi_connected = False
        print("❌ WiFi连接失败")
        return False

# ====================== HTML页面（修复缩略图+显示4张照片）======================
def get_html():
    temp, humi = read_dht11_safe()
    soil_raw, soil_moist = read_soil_safe()
    
    # 【修改】显示最新的4张照片
    photo_list = [f for f in os.listdir() if f.startswith("photo_") and f.endswith(".jpg")]
    photo_list.sort(reverse=True)
    photo_list = photo_list[:4]
    
    auto_status = "已启动" if auto_collect_enabled else "已停止"
    auto_btn_text = "停止自动采集" if auto_collect_enabled else "启动自动采集"
    auto_btn_class = "btn-red" if auto_collect_enabled else "btn-blue"
    wifi_status_text = "已连接" if wifi_connected else "已断开"
    
    # 最新照片缩略图HTML（增加加载错误处理，避免裂图）
    thumb_html = ""
    if photo_list:
        thumb_html = "<div style='display:flex;gap:15px;flex-wrap:wrap;justify-content:center;'>"
        for photo in photo_list:
            thumb_html += f"""
            <div style='text-align:center;'>
                <a href='/photo?name={photo}' target='_blank'>
                    <img src='/photo?name={photo}' alt='{photo}' style='width:150px;height:112px;object-fit:cover;border-radius:6px;border:1px solid #eee;' onerror='this.style.display="none";this.nextElementSibling.style.display="block";'>
                    <p style='display:none;color:#999;font-size:12px;margin-top:40px;'>加载失败</p>
                </a>
                <p style='font-size:12px;color:#666;margin-top:4px;'>{photo[:20]}...</p>
                <a href='/download?name={photo}' class='btn btn-blue' style='padding:3px 8px;font-size:12px;margin-top:5px;display:inline-block;' download>📥 下载</a>
            </div>
            """
        thumb_html += "</div>"
    else:
        thumb_html = "<p style='color:#666;text-align:center;'>暂无采集记录，点击「一键采集」开始</p>"
    
    # 准实时画面HTML
    live_html = f"<img id='live-img' src='/live?ts={int(time.time())}' alt='准实时画面' style='max-width:100%;border-radius:8px;'>"
    
    html = f"""
    <html>
        <head>
            <meta charset='UTF-8'>
            <meta name='viewport' content='width=device-width, initial-scale=1.0'>
            <title>智能苗圃监测系统</title>
            <style>
                *{{margin:0;padding:0;box-sizing:border-box;font-family:Arial,sans-serif;}}
                body{{background:#f5f5f5;padding:20px;max-width:900px;margin:0 auto;}}
                .header{{text-align:center;margin-bottom:20px;}}
                .panel{{background:#fff;border-radius:8px;padding:20px;margin-bottom:15px;box-shadow:0 2px 4px rgba(0,0,0,0.1);}}
                .panel-title{{font-size:18px;font-weight:bold;margin-bottom:15px;padding-bottom:8px;border-bottom:1px solid #eee;}}
                .status-bar{{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:15px;padding:10px;background:#f8f9fa;border-radius:6px;}}
                .data-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:20px;}}
                .data-card{{background:#f8f9fa;padding:15px;border-radius:6px;text-align:center;}}
                .data-card .label{{font-size:12px;color:#666;margin-bottom:5px;}}
                .data-card .value{{font-size:20px;font-weight:bold;}}
                .btn-group{{display:flex;gap:10px;flex-wrap:wrap;justify-content:center;margin-bottom:10px;}}
                .btn{{padding:10px 20px;border:none;border-radius:6px;color:#fff;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block;}}
                .btn-green{{background:#28a745;}}
                .btn-blue{{background:#007bff;}}
                .btn-red{{background:#dc3545;}}
                .btn-ai{{background:#20c997;}}
                .stream-box{{text-align:center;margin:15px 0;}}
                .thumb-box{{margin-top:15px;}}
                .tip{{text-align:center;color:#666;font-size:12px;margin-top:8px;}}
                @media (max-width:600px){{
                    .data-grid{{grid-template-columns:1fr 1fr;}}
                }}
            </style>
        </head>
        <body>
            <div class='header'>
                <h2>智能苗圃监测与AI预警系统</h2>
                <p>AI高清拍照+准实时画面</p>
            </div>

            <div class='panel'>
                <div class='panel-title'>系统状态</div>
                <div class='status-bar'>
                    <div>WiFi状态：{wifi_status_text}</div>
                    <div>自动采集：{auto_status}</div>
                    <div>设备IP：{device_ip}</div>
                </div>

                <div class='data-grid'>
                    <div class='data-card'>
                        <div class='label'>环境温度</div>
                        <div class='value'>{temp if temp else '--'} ℃</div>
                    </div>
                    <div class='data-card'>
                        <div class='label'>环境湿度</div>
                        <div class='value'>{humi if humi else '--'} %RH</div>
                    </div>
                    <div class='data-card'>
                        <div class='label'>土壤湿度</div>
                        <div class='value'>{soil_moist if soil_moist else '--'} %</div>
                    </div>
                    <div class='data-card'>
                        <div class='label'>土壤原始值</div>
                        <div class='value'>{soil_raw if soil_raw else '--'}</div>
                    </div>
                </div>

                <div class='btn-group'>
                    <a href='/single' class='btn btn-green'>📸 一键采集(AI高清)</a>
                    <a href='{AI_PAGE_URL}' target='_blank' class='btn btn-ai'>🤖 打开AI识别页面</a>
                    <a href='/auto_toggle' class='btn {auto_btn_class}'>{auto_btn_text}</a>
                    <a href='/' class='btn btn-blue'>🔄 刷新数据</a>
                </div>
            </div>

            <div class='panel'>
                <div class='panel-title'>📹 准实时摄像头画面 (3秒自动刷新)</div>
                <div class='stream-box'>
                    {live_html}
                </div>
                <div class='tip'>准实时模式，流畅不占带宽，拍照自动切高清</div>
            </div>

            <div class='panel'>
                <div class='panel-title'>🖼️ 最新采集照片 (点击查看大图/下载，共4张)</div>
                <div class='thumb-box'>
                    {thumb_html}
                </div>
            </div>

            <!-- JS自动刷新准实时画面 -->
            <script>
                window.onload = function() {{
                    setInterval(function() {{
                        const liveImg = document.getElementById('live-img');
                        liveImg.src = '/live?ts=' + new Date().getTime();
                    }}, {LIVE_REFRESH_INTERVAL});
                }};
            </script>
        </body>
    </html>
    """
    return html

# ====================== 【优化】网页服务器，大文件传输优化 ======================
def web_server():
    global auto_collect_enabled, last_collect_time, latest_photo, latest_live_frame
    
    addr = socket.getaddrinfo('0.0.0.0', 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(3)
    print("✅ 极速网页服务器已启动！\n")

    while True:
        # 后台更新准实时画面
        update_live_frame()
        
        # 自动采集逻辑
        current_time = time.time()
        if wifi_connected and auto_collect_enabled and (current_time - last_collect_time >= AUTO_COLLECT_INTERVAL):
            print("\n===== 自动采集触发 =====")
            photo_name, _ = take_photo_ai()
            if photo_name:
                latest_photo = photo_name
                print("✅ 自动采集完成")
            last_collect_time = current_time
        
        # 网页请求处理
        try:
            conn, addr = s.accept()
            conn.settimeout(10.0) # 延长超时时间，大图片传输需要更长时间
            request = conn.recv(1024).decode()
            
            if "GET / " in request:
                response = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n{get_html()}"
                conn.send(response.encode())
            elif "GET /live" in request:
                if latest_live_frame:
                    conn.send(f"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: {len(latest_live_frame)}\r\n\r\n".encode())
                    conn.send(latest_live_frame)
                else:
                    conn.send("HTTP/1.1 404 Not Found\r\n\r\n".encode())
            elif "GET /single" in request:
                print("\n===== 单次采集触发 =====")
                photo_name, _ = take_photo_ai()
                if photo_name:
                    latest_photo = photo_name
                    print("✅ 单次采集完成")
                response = "HTTP/1.1 302 Found\r\nLocation: /\r\n\r\n"
                conn.send(response.encode())
            elif "GET /auto_toggle" in request:
                auto_collect_enabled = not auto_collect_enabled
                if auto_collect_enabled:
                    last_collect_time = time.time()
                    print("✅ 自动采集已启动")
                else:
                    print("✅ 自动采集已停止")
                response = "HTTP/1.1 302 Found\r\nLocation: /\r\n\r\n"
                conn.send(response.encode())
            elif "GET /photo?name=" in request:
                filename = request.split("name=")[1].split(" ")[0]
                try:
                    with open(filename, "rb") as f:
                        img_data = f.read()
                    # 增加缓存，减少重复传输
                    conn.send(f"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nCache-Control: max-age=3600\r\nContent-Length: {len(img_data)}\r\n\r\n".encode())
                    # 分块发送大文件，避免内存溢出
                    chunk_size = 1024
                    for i in range(0, len(img_data), chunk_size):
                        conn.send(img_data[i:i+chunk_size])
                except Exception as e:
                    print(f"图片加载失败: {filename}, 错误: {e}")
                    conn.send("HTTP/1.1 404 Not Found\r\n\r\n<h1>文件不存在</h1>".encode())
            elif "GET /download?name=" in request:
                filename = request.split("name=")[1].split(" ")[0]
                try:
                    with open(filename, "rb") as f:
                        img_data = f.read()
                    conn.send(f"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename={filename}\r\nContent-Length: {len(img_data)}\r\n\r\n".encode())
                    conn.send(img_data)
                except:
                    conn.send("HTTP/1.1 404 Not Found\r\n\r\n<h1>文件不存在</h1>".encode())
            conn.close()
        except Exception as e:
            if "ETIMEDOUT" not in str(e) and "ECONNRESET" not in str(e):
                print(f"服务器错误: {e}")
            try:
                conn.close()
            except:
                pass
        # 每次循环回收一次内存
        gc.collect()

# ====================== 主程序入口 ======================
if __name__ == "__main__":
    try:
        # 启动前清理内存
        gc.collect()
        # 1. 初始化为准实时模式
        switch_camera_mode("live")
        # 2. 连接WiFi
        connect_wifi()
        # 3. 启动网页服务器
        web_server()
    except KeyboardInterrupt:
        print("程序已停止")
        camera.deinit()
    except Exception as e:
        print(f"系统错误: {e}")
        camera.deinit()
        machine.reset()
