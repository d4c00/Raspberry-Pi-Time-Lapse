# 树莓派作为客户端进行延时摄影
可以自动“手动长曝光”与“驱动自动”之中切换，并且“手动长曝光”模式中可以自动调整曝光时间，自动通过https安全上传。
如果断网会临时存储在本地，并在网络恢复后会自动安全上传。
树莓派除了最新型号，大部分都不带rtc模块，在上电时如果没网，无法进行ntp，照片命名时间会变成1970年，可以考虑购买rtc模块或者开机时连一下wifi，待ntp完成后再断网。

## 用法：
### 在树莓派上运行的程序：
安装相应的库后，测试 time-lapse.py 文件能正常运行后
```bash
crontab -e
```
末尾添加
```cron
@reboot python3 /你的路径/time-lapse.py
```
即可上电就开始运行

### 接收图片的服务端:
在docker中启动
```bash
docker run -d \
  --name python_rpi-upload-srv \
  --restart=unless-stopped \
  -v /mnt/sdb/docker/python/rpi/time-lapse:/usr/src/myapp \
  -w /usr/src/myapp \
  -p 80:80 \
  python:3 \
  bash -c "\
    pip install --no-cache-dir Flask Pillow || true && \
    id -u appuser >/dev/null 2>&1 || useradd -u 1000 -m appuser && \
    exec su appuser -c 'python /usr/src/myapp/upload-srv.py'"
```
在`服务端`目录内，还有生成视频与给图片打时间水印的程序：`生成延时摄影.py` `生成延时摄影.py`

### 用nginx反向代理接收端
```nginx‘s default.conf
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name your.domain;

    # 证书配置
    ssl_certificate /etc/letsencrypt/live/your.domain/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your.domain/privkey.pem;

    # SSL 配置
    ssl_session_timeout 5m;
    ssl_session_cache shared:SSL:10m;
    ssl_session_tickets off;
    ssl_protocols TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305';
    ssl_prefer_server_ciphers on;
    ssl_stapling on;
    ssl_stapling_verify on;
    ssl_dhparam /etc/nginx/certs/dhparam4096.pem;

    # HSTS
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;

    location / {
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_pass http://localhost:80/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;

        # 上传文件大小限制
        client_max_body_size 10M;

        # 缓冲区
        proxy_buffer_size   512k;
        proxy_buffers   8 1024k;
        proxy_busy_buffers_size   1024k;
        proxy_max_temp_file_size 0;
    }
}
```
