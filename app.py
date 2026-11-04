# -*- coding: utf-8 -*-
"""Web 健康检查页（Render / Railway 等平台需要一个 HTTP 端口）。"""

import os

from flask import Flask, abort, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(BASE_DIR, "img")

app = Flask(__name__)

FORK_REPO = "https://github.com/lhl77/Save-Restricted-Bot"

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Save Restricted Bot（中文增强版 by LHL）</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root { color-scheme: light dark; }
    body {
      margin: 0; min-height: 100vh; display: flex;
      align-items: center; justify-content: center;
      font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: antiquewhite; color: #333;
    }
    .card {
      background: #fff; padding: 32px 40px; border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0,0,0,.12); max-width: 520px; text-align: center;
    }
    h1 { font-size: 22px; margin: 0 0 8px; }
    p { margin: 8px 0; line-height: 1.6; color: #666; }
    .ok { display: inline-block; margin-top: 8px; padding: 4px 12px;
          border-radius: 999px; background: #e7f7ec; color: #1f9d55; font-size: 13px; }
    a { color: #2563eb; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .links { margin-top: 18px; font-size: 14px; }
    .logo { width: 96px; height: 96px; object-fit: contain; margin-bottom: 12px; }
  </style>
</head>
<body>
  <div class="card">
    <img class="logo" src="/img/1.png" alt="logo"
         onerror="this.style.display='none'">
    <h1>Save Restricted Bot（中文增强版 by LHL）</h1>
    <p>Telegram 受限内容转发机器人正在运行。</p>
    <span class="ok">服务状态：正常</span>
    <div class="links">
      <a href="__FORK__" target="_blank" rel="noopener">Github 仓库</a>
    </div>
  </div>
</body>
</html>""".replace("__FORK__", FORK_REPO)


@app.route("/")
def home():
    return PAGE


@app.route("/img/<path:name>")
def img(name):
    """把仓库里的 img/ 目录直接当静态资源用（机器人头像、README 头图等）。"""
    if not os.path.isdir(IMG_DIR):
        abort(404)
    return send_from_directory(IMG_DIR, name)


@app.route("/healthz")
def health():
    return {"status": "ok", "service": "save-restricted-bot"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)