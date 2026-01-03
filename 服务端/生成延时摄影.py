import os
import re
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont, ImageStat
import subprocess

# --- 用户可配置参数 ---
INPUT_DIR_NAME = "uploads"                                          # 输入图片存放的目录名称
OUTPUT_ROOT_DIR = "output"                                          # 视频和中间文件的输出根目录
FRAMERATE = 60                                                      # 合成视频的帧率（每秒播放的帧数）
FRAME_SKIP = 1                                                      # 抽帧步长，1表示处理每一张图，2表示每隔一张处理
BRIGHTNESS_THRESHOLD = 5                                            # 亮度过滤阈值，低于此平均亮度的图片将被跳过
ENABLE_BRIGHTNESS_CHECK = False                                     # 是否开启亮度检测功能 True or False
FILE_INDEX_WIDTH = 9                                                # 临时图片文件名的数字补全位数，影响ffmpeg读取顺序
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"  # 字体文件绝对路径
FONT_SIZE = 75                                                      # 时间戳字体的大小
TEXT_COLOR = (255, 255, 255)                                        # 时间戳文字颜色 (RGB)
SHADOW_COLOR = (0, 0, 0)                                            # 时间戳阴影/描边颜色 (RGB)
SHADOW_WIDTH = 3                                                    # 阴影或描边的粗细程度
POSITION = (10, 10)                                                 # 时间戳在图片上的起始位置坐标 (x, y)
TIMEZONE_OFFSET_HOURS = 8                                           # 时区修正小时数，将文件名中的UTC时间转为本地时间
ROTATE_DEGREES = 0                                                  # 图片旋转角度（顺时针）
# --- 配置结束 ---

def create_timelapse_with_timestamp():
    print("======== 启动时间流视频生成器 ========")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_root_dir = os.path.join(script_dir, INPUT_DIR_NAME)
    output_root_dir = os.path.join(script_dir, OUTPUT_ROOT_DIR)

    if not os.path.exists(input_root_dir):
        print(f"错误: 输入目录 '{input_root_dir}' 不存在。")
        return

    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except IOError:
        print(f"无法加载字体: {FONT_PATH}")
        return

    device_dirs = [d for d in os.listdir(input_root_dir) if os.path.isdir(os.path.join(input_root_dir, d))]

    for device_id in sorted(device_dirs):
        input_dir = os.path.join(input_root_dir, device_id)
        processed_images_dir = os.path.join(output_root_dir, device_id, "time-lapse", "processed_images")
        os.makedirs(processed_images_dir, exist_ok=True)

        image_files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(".jpg")])
        if not image_files:
            continue

        all_target_times = []
        processed_count = 0

        for i, filename in enumerate(image_files):
            if (i % FRAME_SKIP) != 0:
                continue

            match = re.match(r"pic_(\d+)_?(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.jpg", filename)
            if not match:
                continue

            input_path = os.path.join(input_dir, filename)

            try:
                with Image.open(input_path) as img:
                    if ROTATE_DEGREES % 360 != 0:
                        img = img.rotate(ROTATE_DEGREES, expand=True)

                    if ENABLE_BRIGHTNESS_CHECK:
                        grayscale = img.convert("L")
                        stat = grayscale.getextrema()
                        if stat[1] < BRIGHTNESS_THRESHOLD:
                            continue

                        avg_brightness = ImageStat.Stat(grayscale).mean[0]
                        if avg_brightness < BRIGHTNESS_THRESHOLD:
                            continue

                    date_str = match.group(2)
                    time_str = match.group(3).replace("-", ":")
                    utc_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
                    local_dt = utc_dt + timedelta(hours=TIMEZONE_OFFSET_HOURS)
                    timestamp_text = local_dt.strftime("%Y-%m-%d %H:%M:%S (UTC+8)")

                    draw = ImageDraw.Draw(img)
                    for dx in range(-SHADOW_WIDTH, SHADOW_WIDTH + 1):
                        for dy in range(-SHADOW_WIDTH, SHADOW_WIDTH + 1):
                            if dx != 0 or dy != 0:
                                draw.text(
                                    (POSITION[0] + dx, POSITION[1] + dy),
                                    timestamp_text,
                                    font=font,
                                    fill=SHADOW_COLOR,
                                )
                    draw.text(POSITION, timestamp_text, font=font, fill=TEXT_COLOR)

                    save_name = f"processed_{processed_count:0{FILE_INDEX_WIDTH}d}.jpg"
                    output_path = os.path.join(processed_images_dir, save_name)
                    img.save(output_path)

                    all_target_times.append(local_dt)
                    processed_count += 1
            except Exception as e:
                print(f"[{device_id}] 处理图片出错: {filename} - {e}")

        if not all_target_times:
            if os.path.exists(processed_images_dir):
                os.rmdir(processed_images_dir)
            continue

        start_str = min(all_target_times).strftime("%Y-%m-%d_%H-%M-%S")
        end_str = max(all_target_times).strftime("%Y-%m-%d_%H-%M-%S")
        output_video_path = os.path.join(
            output_root_dir,
            device_id,
            "time-lapse",
            f"{start_str}~{end_str}_timelapse_skip{FRAME_SKIP}.mp4",
        )

        ffmpeg_input_pattern = f"processed_%0{FILE_INDEX_WIDTH}d.jpg"
        ffmpeg_command = [
            "ffmpeg",
            "-y",
            "-framerate",
            str(FRAMERATE),
            "-i",
            os.path.join(processed_images_dir, ffmpeg_input_pattern),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            output_video_path,
        ]

        try:
            subprocess.run(ffmpeg_command, check=True, capture_output=True, text=True)
            print(f"✅ [{device_id}] 视频生成完成")
        except subprocess.CalledProcessError as e:
            print(f"❌ [{device_id}] ffmpeg 失败: {e.stderr}")
        finally:
            for f in os.listdir(processed_images_dir):
                os.remove(os.path.join(processed_images_dir, f))
            os.rmdir(processed_images_dir)

    print("\n🎉 所有设备处理完成")

if __name__ == "__main__":
    create_timelapse_with_timestamp()
