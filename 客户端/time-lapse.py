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

DEVICE_ID = "01"                             #设备id
DEVICE_TOKEN = "cxv-$...21cvn"               #设备认证用TOKEN
SERVER_SCHEME = "https"
SERVER_DOMAIN = "your.domain"
SERVER_PORT = 443

CAPTURE_INTERVAL = 5.0
JPEG_QUALITY = 95
RESOLUTION_MODE = "MAX"

UPLOAD_RETRIES = 3
DEGRADED_UPLOAD_RETRIES = 1

TARGET_BRIGHTNESS = 96
BRIGHTNESS_DEADBAND = 24
STEP_SPEED_UP = 0.9
STEP_SPEED_DOWN = 0.3
BRIGHTNESS_LOW_THRESHOLD = 64
BRIGHTNESS_HIGH_THRESHOLD = 180

MIN_EXPOSURE = 0.25
MAX_EXPOSURE = 112.0
MANUAL_ISO = 1600
AUTOFOCUS_EVERY_N_PHOTOS = 16
MANUAL_WAIT_MULTIPLIER = 1.0

MAX_CACHED_PHOTOS = 100
UPLOAD_TIMEOUT = 30
UPLOAD_WORKERS = 6
RETRY_DELAY = 1

DISK_SUBDIR_NAME = "time-lapse"
DISK_RESERVE_RATIO = 0.05
DISK_SORT_BY_MTIME = True
DISK_FLUSH_ORDER_BY_OLDEST = True

ROTATE_ENABLE = True
ROTATE_ANGLE = 180

# ==========================================
# --- 配置结束 ---
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

degraded_mode = False
degraded_lock = threading.Lock()

flush_lock = threading.Lock()  # 磁盘补传互斥锁

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
        self.lock = threading.Lock()

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
            except Exception:
                pass
        if DISK_SORT_BY_MTIME:
            files.sort()
        for _, p in files:
            if self._space_ok():
                break
            try:
                os.remove(p)
                print(f"[{get_log_time()}] [Disk] 删除旧文件: {os.path.basename(p)}")
            except Exception:
                break

    def save(self, item):
        with self.lock:
            self._cleanup()
            p = os.path.join(self.path, item["name"])
            with open(p, "wb") as f:
                f.write(item["data"])
            print(f"[{get_log_time()}] [Disk] 已保存: {item['name']}")

    def flush_if_needed(self, uploader, max_attempts=UPLOAD_RETRIES):
        if not flush_lock.acquire(blocking=False):
            return  # 其他线程正在补传，直接返回

        try:
            names = os.listdir(self.path)
            if DISK_FLUSH_ORDER_BY_OLDEST:
                names.sort(key=lambda n: os.path.getmtime(os.path.join(self.path, n)))

            for name in names:
                p = os.path.join(self.path, name)
                for attempt in range(1, max_attempts + 1):
                    try:
                        with open(p, "rb") as f:
                            data = f.read()
                        if uploader({"name": name, "data": data}):
                            os.remove(p)
                            print(f"[{get_log_time()}] [Flush] 成功补传: {name}")
                            break
                        else:
                            print(f"[{get_log_time()}] [Flush] 重试 {attempt}/{max_attempts} 等待 {RETRY_DELAY}s: {name}")
                            time.sleep(RETRY_DELAY)
                    except Exception as e:
                        print(f"[{get_log_time()}] [Flush] 异常 {attempt}/{max_attempts}: {name} | {e}")
                        time.sleep(RETRY_DELAY)
                    if attempt == max_attempts:
                        print(f"[{get_log_time()}] [Flush] 达到最大重试，停止补传: {name}")
                        return False
            return True
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

    print(f"[{get_log_time()}] [HTTP] 正在 POST: {SERVER_URL}/upload | 文件: {item['name']}")
    try:
        r = session.post(
            f"{SERVER_URL}/upload",
            data=item["data"],
            headers=headers,
            timeout=UPLOAD_TIMEOUT
        )
        if r.status_code == 200:
            print(f"[{get_log_time()}] [HTTP] 上传成功: {item['name']}")
            return True
        else:
            print(f"[{get_log_time()}] [HTTP] 上传失败: {item['name']} | 状态码: {r.status_code} | 响应: {r.text[:200]}")
            return False
    except requests.Timeout:
        print(f"[{get_log_time()}] [HTTP] 上传超时: {item['name']}")
        return False
    except requests.RequestException as e:
        print(f"[{get_log_time()}] [HTTP] 上传异常: {item['name']} | 异常信息: {e}")
        return False

def upload_worker(item):
    global degraded_mode
    session = requests.Session()

    with degraded_lock:
        local_degraded = degraded_mode

    # 降级模式上传，只尝试 DEGRADED_UPLOAD_RETRIES 次
    if local_degraded:
        for attempt in range(1, DEGRADED_UPLOAD_RETRIES + 1):
            success = upload_once(item, session)
            if success:
                print(f"[{get_log_time()}] [Upload] 降级成功: {item['name']}")
                disk_manager.flush_if_needed(lambda i: upload_once(i, session), max_attempts=DEGRADED_UPLOAD_RETRIES)
                with degraded_lock:
                    degraded_mode = False
                photo_queue.task_done()
                return
            else:
                print(f"[{get_log_time()}] [Upload] 降级重试 {attempt}/{DEGRADED_UPLOAD_RETRIES} 等待 {RETRY_DELAY}s: {item['name']}")
                time.sleep(RETRY_DELAY)
        # 尝试次数用完仍失败，保存到磁盘，保持降级模式
        disk_manager.save(item)
        print(f"[{get_log_time()}] [Upload] 降级上传失败，保持降级模式: {item['name']}")
        photo_queue.task_done()
        return

    # 非降级模式保持原来的 UPLOAD_RETRIES
    for attempt in range(1, UPLOAD_RETRIES + 1):
        success = upload_once(item, session)
        if success:
            print(f"[{get_log_time()}] [Upload] 成功 ({attempt}/{UPLOAD_RETRIES}): {item['name']}")
            photo_queue.task_done()
            return
        else:
            print(f"[{get_log_time()}] [Upload] 重试 {attempt}/{UPLOAD_RETRIES} 等待 {RETRY_DELAY}s: {item['name']}")
            time.sleep(RETRY_DELAY)

    with degraded_lock:
        degraded_mode = True
    disk_manager.save(item)
    print(f"[{get_log_time()}] [Upload] 进入降级模式: {item['name']}")
    photo_queue.task_done()

def upload_task():
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as ex:
        while True:
            item = photo_queue.get()
            ex.submit(upload_worker, item)

def camera_task():
    pic2 = Picamera2()

    res_map = {
        "MAX": (4608, 2592),
        "QSXGA": (2560, 1920),
        "FHD": (1920, 1080),
        "UXGA": (1600, 1200),
        "VGA": (640, 480)
    }

    active_res = None
    photo_count_since_af = 0
    mode = "AUTO"
    current_exposure = MIN_EXPOSURE

    while True:
        try:
            loop_start = time.perf_counter()
            target_wait = max(
                config_data["interval"],
                current_exposure * MANUAL_WAIT_MULTIPLIER
            ) if mode == "MANUAL" else config_data["interval"]
            elapsed = time.perf_counter() - loop_start
            time.sleep(max(0.1, target_wait - elapsed))

            target_res = res_map.get(config_data["resolution"], res_map["MAX"])
            if active_res != target_res:
                try:
                    pic2.stop()
                except:
                    pass
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
                    "AnalogueGain": 16.0,
                    "AfMode": 0
                })

            buf = BytesIO()
            pic2.capture_file(buf, format="jpeg")
            img_data = buf.getvalue()
            img_data = rotate_image_if_needed(img_data)

            brightness = get_brightness(BytesIO(img_data))
            print(f"[{get_log_time()}] [Cam] 模式:{mode} | 亮度:{brightness:.1f} | 曝光:{current_exposure if mode=='MANUAL' else 'AUTO'}s")

            if mode == "AUTO":
                photo_count_since_af += 1
                if photo_count_since_af >= config_data["af_every_n"]:
                    pic2.autofocus_cycle()
                    photo_count_since_af = 0
                if brightness < BRIGHTNESS_LOW_THRESHOLD:
                    print(f"[{get_log_time()}] [Cam] >>> 亮度过低，切换至手动模式")
                    mode = "MANUAL"
                    current_exposure = MIN_EXPOSURE
            else:
                if brightness > BRIGHTNESS_HIGH_THRESHOLD and current_exposure <= MIN_EXPOSURE:
                    print(f"[{get_log_time()}] [Cam] >>> 最小曝光仍然过亮，切换回自动模式")
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
                            if delta > max_step:
                                delta = max_step
                        else:
                            max_step = current_exposure * STEP_SPEED_DOWN
                            if delta < -max_step:
                                delta = -max_step
                        current_exposure = current_exposure + delta
                        if current_exposure < MIN_EXPOSURE:
                            current_exposure = MIN_EXPOSURE
                        elif current_exposure > MAX_EXPOSURE:
                            current_exposure = MAX_EXPOSURE
                        print(f"[{get_log_time()}] [Cam] 调整曝光至: {current_exposure:.3f}s")
                    else:
                        print(f"[{get_log_time()}] [Cam] 亮度进入死区，保持曝光")

            filename = f"pic_{DEVICE_ID}_{timestamp}.jpg"
            try:
                photo_queue.put_nowait({"name": filename, "data": img_data})
            except Full:
                pass

        except Exception as e:
            print(f"[{get_log_time()}] [Cam] 错误: {e}")
            time.sleep(2)

if __name__ == "__main__":
    print(f"[{get_log_time()}] === Zero 2W 延时摄影启动 ===")

    with degraded_lock:
        degraded_mode = True

    session = requests.Session()

    # 异步补传残留文件
    def flush_disk_background():
        success = disk_manager.flush_if_needed(lambda i: upload_once(i, session))
        with degraded_lock:
            global degraded_mode
            degraded_mode = False
        if success:
            print(f"[{get_log_time()}] [Init] 磁盘残留上传尝试完成，恢复正常模式")
        else:
            print(f"[{get_log_time()}] [Init] 磁盘残留部分文件未上传，仍处于降级模式")

    threading.Thread(target=flush_disk_background, daemon=True).start()

    threading.Thread(target=camera_task, daemon=True).start()
    threading.Thread(target=upload_task, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[{get_log_time()}] 停止")
