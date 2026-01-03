import os
import re
import configparser
import time

from flask import Flask, request, jsonify
from PIL import Image

app = Flask(__name__)

script_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(script_dir, 'upload-srv.ini')

DEFAULT_PORT = 80
RESPONSE_DELAY = 0

PORT = DEFAULT_PORT
DEVICE_CONFIGS = {}

def load_configuration():
    global PORT, DEVICE_CONFIGS
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_file_path)
    DEVICE_CONFIGS.clear()

    for section in config.sections():
        if section == 'settings':
            continue

        device_id = config.get(section, 'device_id', fallback=None)
        if not device_id:
            continue

        base_upload_path = config.get(section, 'upload_folder', fallback='uploads')
        final_path = os.path.join(script_dir, base_upload_path, device_id)

        DEVICE_CONFIGS[device_id] = {
            "device_id": device_id,
            "device_token": config.get(section, 'device_token', fallback=None),
            "max_file_size_bytes": int(
                config.getfloat(section, 'max_file_size_mb', fallback=1.0) * 1024 * 1024
            ),
            "upload_folder": final_path
        }

        os.makedirs(final_path, exist_ok=True)

    PORT = config.getint('settings', 'port', fallback=DEFAULT_PORT)

load_configuration()

JPEG_MAGIC_BYTES = b'\xFF\xD8\xFF'

def is_jpeg_magic_bytes(data):
    return (
        data and
        len(data) >= 4 and
        data[0:3] == JPEG_MAGIC_BYTES and
        0xE0 <= data[3] <= 0xEF
    )

def validate_device_headers():
    device_id = request.headers.get('X-Device-Id')
    device_token = request.headers.get('X-Device-Token')

    if not device_id or not device_token:
        time.sleep(RESPONSE_DELAY)
        return None, jsonify({"status": "error", "message": "missing device id or token"}), 400

    device_cfg = DEVICE_CONFIGS.get(device_id)
    if not device_cfg:
        time.sleep(RESPONSE_DELAY)
        return None, jsonify({"status": "error", "message": "unknown device"}), 403

    if device_token != device_cfg.get("device_token"):
        time.sleep(RESPONSE_DELAY)
        return None, jsonify({"status": "error", "message": "invalid device token"}), 403

    return device_cfg, None, None

@app.route('/upload', methods=['POST'])
def upload_photo():
    FILENAME_PATTERN = r"^pic_(\d{2})_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.jpg$"

    device_cfg, err_resp, err_code = validate_device_headers()
    if err_resp:
        return err_resp, err_code

    image_data = request.data
    if not image_data:
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "empty body"}), 400

    if len(image_data) > device_cfg["max_file_size_bytes"]:
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "file too large"}), 413

    if not is_jpeg_magic_bytes(image_data):
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "not jpeg"}), 415

    filename_header = request.headers.get('X-Filename')
    if not filename_header:
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "missing filename header"}), 400

    base_filename = os.path.basename(filename_header)
    if not re.match(FILENAME_PATTERN, base_filename):
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "invalid filename format"}), 400

    filepath = os.path.join(device_cfg["upload_folder"], base_filename)
    with open(filepath, 'wb') as f:
        f.write(image_data)

    if os.path.getsize(filepath) != len(image_data):
        os.remove(filepath)
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "size mismatch"}), 400

    try:
        img = Image.open(filepath)
        img.verify()
        img.close()
    except Exception:
        os.remove(filepath)
        time.sleep(RESPONSE_DELAY)
        return jsonify({"status": "error", "message": "invalid jpeg"}), 400

    time.sleep(RESPONSE_DELAY)
    return jsonify({"status": "success", "message": f"file {base_filename} uploaded"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
