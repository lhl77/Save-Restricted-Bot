# pyrogram 2.0.106 在 Python 3.14 下会因 asyncio.get_event_loop() 变动而报错，
# 这里固定使用 3.11 版本。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# tgcrypto 部分平台没有预编译 wheel，需要编译工具；ffmpeg 用于上传前重新编码
COPY requirements.txt /app/
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ffmpeg \
    && pip install -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY . /app

# 同时启动 Web 健康检查页与机器人主进程
CMD ["sh", "-c", "python3 app.py & python3 main.py"]