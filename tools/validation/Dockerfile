FROM python:3.12-slim
RUN sed -i "s@deb.debian.org/debian@mirrors.cloud.aliyuncs.com/debian@g; s@security.debian.org/debian-security@mirrors.cloud.aliyuncs.com/debian-security@g" /etc/apt/sources.list.d/debian.sources && apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates fonts-noto-cjk && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir -i http://mirrors.cloud.aliyuncs.com/pypi/simple/ --trusted-host mirrors.cloud.aliyuncs.com playwright==1.55.0
ENV HOME=/tmp PLAYWRIGHT_BROWSERS_PATH=0
USER 65534:65534
