import time
import threading
import requests
from datetime import datetime, UTC
from queue import Queue, Full
from io import BytesIO
from picamera2 import Picamera2
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageStat
import os

# ==========================================
# --- 用户配置 ---
# ==========================================

# 设备标识与鉴权
DEVICE_ID = "01"               # 设备唯一编号，用于服务端区分照片来源
DEVICE_TOKEN = "xv-$...21cvn"  # 访问令牌，用于上传时的身份校验，确保安全性

# 服务端连接配置
SERVER_SCHEME = "https"        # 通讯协议 (http 或 https)
SERVER_DOMAIN = "your.domain" # 服务器域名或IP地址
SERVER_PORT = 443             # 服务器端口号

# 拍摄基本设置
CAPTURE_INTERVAL = 5.0         # 拍摄间隔（秒），即每隔多少秒拍一张
JPEG_QUALITY = 95              # 图片压缩质量 (1-100)，越高越清晰但文件越大
RESOLUTION_MODE = "MAX"        # 分辨率模式 (对应脚本中的分辨率映射表，如 MAX, FHD 等)

# 实时上传重试配置（当网络波动时的处理）
UPLOAD_RETRIES = 2             # 实时上传失败后的重试次数
UPLOAD_TIMEOUT = 30            # 上传超时时间（秒），超过此时间则认为单次请求失败
UPLOAD_WORKERS = 4             # 并行上传的线程数，增加此值可加速排队中的照片上传

# 补传逻辑独立配置（当从断网恢复或处理磁盘残留照片时）
FLUSH_RETRIES_PER_FILE = 3     # 补传阶段每个文件失败后的重试次数
RETRY_DELAY = 2                # 失败后的等待延迟（秒），防止连续快速失败重试

# 亮度与曝光控制（自动切换黑夜模式逻辑）
TARGET_BRIGHTNESS = 64         # 目标平均亮度值 (0-255)
BRIGHTNESS_DEADBAND = 24       # 亮度死区，当前亮度在此偏差范围内时不调整曝光，防止反复震荡
STEP_SPEED_UP = 0.9            # 调亮（增加曝光）的步进速度系数，越大调整越快
STEP_SPEED_DOWN = 0.3          # 调暗（减少曝光）的步进速度系数，通常设小一点以平滑过渡
BRIGHTNESS_LOW_THRESHOLD = 48  # 进入手动模式（长曝光模式）的亮度触发下限
BRIGHTNESS_HIGH_THRESHOLD = 155# 退出手动模式返回自动模式的亮度触发上限

# 曝光参数限制
MIN_EXPOSURE = 0.25            # 最小曝光时间（秒）
MAX_EXPOSURE = 112.0           # 最大曝光时间（秒），用于极暗环境长曝光
MANUAL_ISO = 1600              # 手动模式下的固定 ISO (增益)
AUTOFOCUS_EVERY_N_PHOTOS = 16  # 每隔多少张照片执行一次自动对焦循环
MANUAL_WAIT_MULTIPLIER = 1.0   # 长曝光时的额外等待系数，确保曝光完成后再进行下一张拍摄

# 内存缓冲区设置
MAX_CACHED_PHOTOS = 100        # 内存队列中最多缓存的照片张数，满了会丢弃新帧

# 磁盘存储设置
DISK_SUBDIR_NAME = "time-lapse" # 存盘失败或降级模式下照片存放的本地文件夹名
DISK_RESERVE_RATIO = 0.05       # 磁盘保留空间比例 (0.05 代表 5%)，低于此比例会删除旧文件
DISK_SORT_BY_MTIME = True       # 磁盘清理时是否按文件修改时间排序
DISK_FLUSH_ORDER_BY_OLDEST = True # 补传时是否优先上传最旧的照片

# 图像物理变换
ROTATE_ENABLE = True           # 是否开启图像旋转
ROTATE_ANGLE = 180             # 旋转角度 (0, 90, 180, 270)，常用于摄像头倒挂安装

# 启动与网络检测
STARTUP_NETWORK_WAIT_MAX = 120 # 启动时最长等待网络就绪的时间（秒）
NETWORK_CHECK_INTERVAL = 5     # 启动时检查网络连通性的循环间隔（秒）

# ==========================================
# --- 核心逻辑 ---
# ==========================================

SERVER_URL = f"{SERVER_SCHEME}://{SERVER_DOMAIN}:{SERVER_PORT}"
photo_queue = Queue(maxsize=MAX_CACHED_PHOTOS)

config_data = {
    "interval": CAPTURE_INTERVAL,
    "quality": JPEG_QUALITY,
    "resolution": RESOLUTION_MODE,
    "af_every_n": AUTOFOCUS_EVERY_N_PHOTOS
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DISK_DIR = os.path.join(BASE_DIR, DISK_SUBDIR_NAME)
os.makedirs(DISK_DIR, exist_ok=True)

# 降级模式标记与锁
degraded_mode = False
degraded_lock = threading.Lock()

# 补传互斥锁：确保同一时间只有一个补传线程在跑，且不被主流程干扰
flush_lock = threading.Lock()

def get_log_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def get_brightness(image_bytes):
    try:
        with Image.open(image_bytes) as img:
            img_small = img.resize((100, 100)).convert("L")
            stat = ImageStat.Stat(img_small)
            return stat.mean[0]
    except Exception:
        return TARGET_BRIGHTNESS

def rotate_image_if_needed(image_bytes):
    if not ROTATE_ENABLE or ROTATE_ANGLE % 360 == 0:
        return image_bytes
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            rotated = img.rotate(-ROTATE_ANGLE, expand=True)
            out = BytesIO()
            rotated.save(out, format="JPEG", quality=JPEG_QUALITY)
            return out.getvalue()
    except Exception:
        return image_bytes

class DiskManager:
    def __init__(self, path, reserve_ratio):
        self.path = path
        self.reserve_ratio = reserve_ratio
        self.op_lock = threading.Lock() # 磁盘物理操作锁
        # 内存中记录磁盘是否有文件，减少listdir调用
        self.has_files = len(os.listdir(self.path)) > 0

    def _space_ok(self):
        st = os.statvfs(self.path)
        free = st.f_bavail * st.f_frsize
        total = st.f_blocks * st.f_frsize
        return free / total >= self.reserve_ratio

    def _cleanup(self):
        files = []
        for name in os.listdir(self.path):
            p = os.path.join(self.path, name)
            try:
                files.append((os.path.getmtime(p), p))
            except Exception: pass
        
        if DISK_SORT_BY_MTIME:
            files.sort()
        
        for _, p in files:
            if self._space_ok(): break
            try:
                os.remove(p)
                print(f"[{get_log_time()}] [Disk] 空间不足，删除旧文件: {os.path.basename(p)}")
            except Exception: break

    def save(self, item):
        with self.op_lock:
            self._cleanup()
            p = os.path.join(self.path, item["name"])
            try:
                with open(p, "wb") as f:
                    f.write(item["data"])
                self.has_files = True # 更新状态
                print(f"[{get_log_time()}] [Disk] 已存盘: {item['name']}")
            except Exception as e:
                print(f"[{get_log_time()}] [Disk] 存盘失败: {e}")

    def flush_to_server(self, session):
        """
        核心补传逻辑：一旦启动，要么清空磁盘，要么在连续失败后放弃。
        """
        if not flush_lock.acquire(blocking=False):
            return # 已经有补传在运行了，跳过

        try:
            print(f"[{get_log_time()}] [Flush] 开始执行补传任务...")
            while True:
                names = os.listdir(self.path)
                if not names:
                    print(f"[{get_log_time()}] [Flush] 磁盘已清空，补传结束。")
                    self.has_files = False # 更新内存状态：已清空
                    with degraded_lock:
                        global degraded_mode
                        degraded_mode = False
                    return

                if DISK_FLUSH_ORDER_BY_OLDEST:
                    names.sort(key=lambda n: os.path.getmtime(os.path.join(self.path, n)))

                # 取出第一个文件尝试上传
                name = names[0]
                p = os.path.join(self.path, name)
                success = False

                for attempt in range(1, FLUSH_RETRIES_PER_FILE + 1):
                    try:
                        with open(p, "rb") as f:
                            data = f.read()
                        
                        if upload_once({"name": name, "data": data}, session):
                            os.remove(p)
                            print(f"[{get_log_time()}] [Flush] 补传成功: {name}")
                            success = True
                            break
                        else:
                            print(f"[{get_log_time()}] [Flush] 补传失败 {attempt}/{FLUSH_RETRIES_PER_FILE}: {name}")
                    except Exception as e:
                        print(f"[{get_log_time()}] [Flush] 读取/删除异常: {e}")
                    
                    time.sleep(RETRY_DELAY)

                if not success:
                    print(f"[{get_log_time()}] [Flush] 文件 {name} 连续失败，判定网络仍不可用，退出补传。")
                    return
        finally:
            flush_lock.release()

disk_manager = DiskManager(DISK_DIR, DISK_RESERVE_RATIO)

def upload_once(item, session):
    headers = {
        "X-Device-Token": DEVICE_TOKEN,
        "X-Device-Id": DEVICE_ID,
        "X-Filename": item["name"],
        "Content-Type": "image/jpeg"
    }
    try:
        r = session.post(
            f"{SERVER_URL}/upload",
            data=item["data"],
            headers=headers,
            timeout=UPLOAD_TIMEOUT
        )
        return r.status_code == 200
    except Exception:
        return False

def upload_worker(item):
    """
    实时上传工作线程
    """
    global degraded_mode
    session = requests.Session()

    # 如果当前处于降级模式，直接存盘，不再尝试实时上传，避免卡死线程池
    with degraded_lock:
        if degraded_mode:
            disk_manager.save(item)
            photo_queue.task_done()
            return

    # 尝试实时上传
    success = False
    for attempt in range(1, UPLOAD_RETRIES + 1):
        if upload_once(item, session):
            print(f"[{get_log_time()}] [Upload] 实时上传成功: {item['name']}")
            success = True
            break
        else:
            if attempt < UPLOAD_RETRIES:
                time.sleep(RETRY_DELAY)

    if not success:
        print(f"[{get_log_time()}] [Upload] 实时上传失败，进入降级模式: {item['name']}")
        disk_manager.save(item)
        with degraded_lock:
            degraded_mode = True
    
    photo_queue.task_done()

def upload_task_loop():
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
        while True:
            item = photo_queue.get()
            ex.submit(upload_worker, item)

def flush_trigger_loop():
    """
    独立线程：定期检查内存标志位。如果标志位显示有文件，尝试触发补传。
    """
    session = requests.Session()
    while True:
        # 使用内存变量检测，替代频繁的 os.listdir
        if disk_manager.has_files:
            disk_manager.flush_to_server(session)
        time.sleep(10)

def camera_task():
    pic2 = Picamera2()
    res_map = {
        "MAX": (4608, 2592), "QSXGA": (2560, 1920), "FHD": (1920, 1080),
        "UXGA": (1600, 1200), "VGA": (640, 480)
    }

    active_res = None
    photo_count_since_af = 0
    mode = "AUTO"
    current_exposure = MIN_EXPOSURE

    while True:
        try:
            loop_start = time.perf_counter()
            target_wait = max(config_data["interval"], current_exposure * MANUAL_WAIT_MULTIPLIER) if mode == "MANUAL" else config_data["interval"]
            elapsed = time.perf_counter() - loop_start
            time.sleep(max(0.1, target_wait - elapsed))

            target_res = res_map.get(config_data["resolution"], res_map["MAX"])
            if active_res != target_res:
                try: pic2.stop()
                except: pass
                cfg = pic2.create_still_configuration(main={"size": target_res})
                pic2.configure(cfg)
                pic2.start()
                pic2.autofocus_cycle()
                active_res = target_res
                mode = "AUTO"

            timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
            pic2.encoder_options = {"quality": config_data["quality"]}

            if mode == "AUTO":
                pic2.set_controls({"AeEnable": True, "AfMode": 2})
            else:
                pic2.set_controls({
                    "AeEnable": False,
                    "ExposureTime": int(current_exposure * 1_000_000),
                    "AnalogueGain": 16.0, "AfMode": 0
                })

            buf = BytesIO()
            pic2.capture_file(buf, format="jpeg")
            img_data = rotate_image_if_needed(buf.getvalue())

            brightness = get_brightness(BytesIO(img_data))
            
            # 曝光调节逻辑
            if mode == "AUTO":
                photo_count_since_af += 1
                if photo_count_since_af >= config_data["af_every_n"]:
                    pic2.autofocus_cycle()
                    photo_count_since_af = 0
                if brightness < BRIGHTNESS_LOW_THRESHOLD:
                    mode = "MANUAL"
                    current_exposure = MIN_EXPOSURE
            else:
                if brightness > BRIGHTNESS_HIGH_THRESHOLD and current_exposure <= MIN_EXPOSURE:
                    mode = "AUTO"
                    current_exposure = MIN_EXPOSURE
                    pic2.autofocus_cycle()
                else:
                    error = brightness - TARGET_BRIGHTNESS
                    if abs(error) >= BRIGHTNESS_DEADBAND:
                        ratio = TARGET_BRIGHTNESS / max(brightness, 1.0)
                        desired_exposure = current_exposure * ratio
                        delta = desired_exposure - current_exposure
                        if delta > 0:
                            max_step = current_exposure * STEP_SPEED_UP
                            if delta > max_step: delta = max_step
                        else:
                            max_step = current_exposure * STEP_SPEED_DOWN
                            if delta < -max_step: delta = -max_step
                        current_exposure = max(MIN_EXPOSURE, min(MAX_EXPOSURE, current_exposure + delta))

            filename = f"pic_{DEVICE_ID}_{timestamp}.jpg"
            try:
                photo_queue.put_nowait({"name": filename, "data": img_data})
            except Full:
                print(f"[{get_log_time()}] [Cam] 队列满，丢弃当前帧")

        except Exception as e:
            print(f"[{get_log_time()}] [Cam] 运行异常: {e}")
            time.sleep(2)

if __name__ == "__main__":
    print(f"[{get_log_time()}] === Zero 2W 延时摄影增强版启动 ===")

    # 启动时默认进入降级模式，直到网络检查通过
    with degraded_lock:
        degraded_mode = True

    def startup_logic():
        session = requests.Session()
        start_time = time.time()
        
        # 1. 等待网络就绪
        while time.time() - start_time < STARTUP_NETWORK_WAIT_MAX:
            try:
                r = session.get(f"{SERVER_URL}/", timeout=5)
                if r.status_code < 500:
                    print(f"[{get_log_time()}] [Init] 网络连通性测试通过")
                    break
            except: pass
            print(f"[{get_log_time()}] [Init] 等待网络中...")
            time.sleep(NETWORK_CHECK_INTERVAL)
        
        # 2. 强制执行一次磁盘补传检测（使用内存变量标记位触发）
        if disk_manager.has_files:
            disk_manager.flush_to_server(session)
        
        print(f"[{get_log_time()}] [Init] 启动流程结束，进入常规模式")

    # 启动所有后台线程
    threading.Thread(target=startup_logic, daemon=True).start()
    threading.Thread(target=camera_task, daemon=True).start()
    threading.Thread(target=upload_task_loop, daemon=True).start()
    threading.Thread(target=flush_trigger_loop, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{get_log_time()}] 用户停止")