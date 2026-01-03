import os
import re
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont

# --- 用户可配置参数 ---
INPUT_DIR_NAME = "uploads"
OUTPUT_ROOT_DIR = "output"

FRAME_SKIP = 1

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE = 75
TEXT_COLOR = (255, 255, 255)
SHADOW_COLOR = (0, 0, 0)
SHADOW_WIDTH = 3
POSITION = (10, 10)

TIMEZONE_OFFSET_HOURS = 8
# --- 配置结束 ---


def add_timestamp_to_images():
    print("======== 启动时间戳图像处理器 ========")
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
    if not device_dirs:
        print(f"在 '{input_root_dir}' 下没有找到任何设备子目录。")
        return

    for device_id in sorted(device_dirs):
        input_dir = os.path.join(input_root_dir, device_id)
        output_dir = os.path.join(output_root_dir, device_id, "time-tag")
        os.makedirs(output_dir, exist_ok=True)

        image_files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(".jpg")])
        if not image_files:
            continue

        processed_count = 0
        for i, filename in enumerate(image_files):
            if (i % FRAME_SKIP) != 0:
                continue

            match = re.match(r"pic_(\d+)_?(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})\.jpg", filename)
            if not match:
                continue

            date_str = match.group(2)
            time_str = match.group(3).replace("-", ":")

            try:
                utc_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
                local_dt = utc_dt + timedelta(hours=TIMEZONE_OFFSET_HOURS)
                
                # 构造时区标签字符串，如 "UTC+8"
                tz_label = f"UTC{'+' if TIMEZONE_OFFSET_HOURS >= 0 else ''}{TIMEZONE_OFFSET_HOURS}"
                
                # 用于图片上绘制的文本
                timestamp_text = local_dt.strftime(f"%Y-%m-%d %H:%M:%S ({tz_label})")
                
                # 用于文件名的字符串（将冒号替换为横杠以符合文件命名规范）
                file_timestamp = local_dt.strftime(f"%Y-%m-%d_%H-%M-%S_({tz_label})")
            except ValueError:
                continue

            input_path = os.path.join(input_dir, filename)
            # 输出文件名：2025-12-20_01-00-00_(UTC+8).jpg
            output_path = os.path.join(output_dir, f"{file_timestamp}.jpg")

            try:
                with Image.open(input_path) as img:
                    draw = ImageDraw.Draw(img)
                    for dx in range(-SHADOW_WIDTH, SHADOW_WIDTH + 1):
                        for dy in range(-SHADOW_WIDTH, SHADOW_WIDTH + 1):
                            if dx != 0 or dy != 0:
                                draw.text((POSITION[0]+dx, POSITION[1]+dy), timestamp_text, font=font, fill=SHADOW_COLOR)
                    draw.text(POSITION, timestamp_text, font=font, fill=TEXT_COLOR)
                    img.save(output_path)
                processed_count += 1
            except Exception as e:
                print(f"[{device_id}] 处理出错: {e}")

        print(f"[{device_id}] 处理完成，共 {processed_count} 张。")


if __name__ == "__main__":
    add_timestamp_to_images()