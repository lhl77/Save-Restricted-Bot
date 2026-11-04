# -*- coding: utf-8 -*-
"""
Save Restricted Bot —— Telegram 受限内容转发机器人（中文增强版 by LHL）

功能概述
--------
把设置了「禁止保存内容」的 Telegram 频道 / 群组 / 论坛话题中的消息，
通过用户会话（每用户自助 /login 登录）下载后，
由机器人重新上传到目标会话。

本仓库（中文增强版 by LHL）：https://github.com/lhl77/Save-Restricted-Bot
原版仓库：                    https://github.com/bipinkrish/Save-Restricted-Bot

运行建议：Python 3.10 ~ 3.13（推荐 3.11），pyrogram==2.0.106
"""

import asyncio
import base64
import inspect
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import uuid
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

from pyrogram import Client, StopTransmission, enums, filters, idle, raw
from pyrogram import utils as pyrogram_utils
from pyrogram.errors import (
    FloodWait,
    InviteHashExpired,
    InviteHashInvalid,
    PasswordHashInvalid,
    PeerIdInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
    UserAlreadyParticipant,
    UsernameInvalid,
    UsernameNotOccupied,
)
from pyrogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

from link_parser import BOT as KIND_BOT
from link_parser import INVITE as KIND_INVITE
from link_parser import PRIVATE as KIND_PRIVATE
from link_parser import parse_links

# --------------------------------------------------------------------------- #
# 0. 常量 / 日志
# --------------------------------------------------------------------------- #

FORK_REPO = "https://github.com/lhl77/Save-Restricted-Bot"
ORIGIN_REPO = "https://github.com/bipinkrish/Save-Restricted-Bot"

# 版本号（日历式：年.月.日，改代码时顺手更新）
BOT_VERSION = "2026.11.04"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SESSION_FILE = os.path.join(BASE_DIR, "sessions.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

# 可以作为相册一次性发送的消息类型
ALBUM_TYPES = {"Photo", "Video", "Document", "Audio"}

# 登录会话的等待时间（秒）
LOGIN_TIMEOUT = 600

# 扫码登录：轮询间隔（秒）与整体等待时间（秒）
# 注意：Telegram 的登录 token 在过期前会重复返回同一个值，
# 所以轮询要快（及时感知扫码），但只在 token 变化时才重画二维码。
QR_POLL_SECONDS = 3
QR_LOGIN_TIMEOUT = 300

try:
    import qrcode as _qrcode
    QR_AVAILABLE = True
except Exception:  # noqa: BLE001
    _qrcode = None
    QR_AVAILABLE = False


def log(text):
    """统一的日志输出（带时间戳，中文）。"""
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), text), flush=True)


def patch_peer_id_range():
    """
    放宽 pyrogram 的超级群组 ID 范围。

    pyrogram 2.0.106 把 channel_id 上限写死在 2^31-1：
        MIN_CHANNEL_ID = -1002147483647
    但 Telegram 现在分配的超级群组 ID 已经更大（例如 3568041317），
    私有群组 / 话题群会直接报：
        ValueError: Peer id invalid: -1003568041317
    把下限放宽到 1e14 即可恢复正常解析（peer 换算公式本身没有位宽限制）。
    """
    try:
        new_min = -100999999999999
        current = getattr(pyrogram_utils, "MIN_CHANNEL_ID", None)
        if current is None or current > new_min:
            pyrogram_utils.MIN_CHANNEL_ID = new_min
            log("已放宽 pyrogram 的超级群组 ID 范围（修复 Peer id invalid）")
    except Exception as exc:  # noqa: BLE001
        log("修补 pyrogram peer id 范围失败：%s" % exc)


patch_peer_id_range()


def patch_system_lang_code():
    """
    修正 pyrogram 的设备指纹缺陷：system_lang_code 被写死成 lang_code。

    pyrogram 2.0.106 的 session.py 里是这么发的：
        system_lang_code=self.client.lang_code,
        lang_code=self.client.lang_code,
    但真实客户端这两项通常不同：
        系统语言 zh-CN  ／  Telegram 语言包 zh-hans
    两者永远相等本身就是一条很明显的「非官方客户端」特征。
    这里把方法源码取出来做一次替换再装回去，让它读取独立的
    client.system_lang_code（由 apply_fingerprint 写入）。
    """
    try:
        from pyrogram.session import session as session_module

        original = session_module.Session.start
        source = textwrap.dedent(inspect.getsource(original))
        target = "system_lang_code=self.client.lang_code"
        if target not in source:
            return  # 已经改过，或版本行为已不同
        source = source.replace(
            target,
            'system_lang_code=getattr(self.client, "system_lang_code", '
            "self.client.lang_code)",
        )
        namespace = dict(session_module.__dict__)
        exec(compile(source, "<pyrogram session.py patched>", "exec"), namespace)
        patched = namespace.get(original.__name__)
        if patched is None:
            return
        session_module.Session.start = patched
        log("已修正客户端指纹：system_lang_code 与 lang_code 分离")
    except Exception as exc:  # noqa: BLE001
        log("修正客户端指纹失败（不影响使用）：%s" % exc)


patch_system_lang_code()


# --------------------------------------------------------------------------- #
# 1. 配置读取：环境变量 > config.json > 默认值
# --------------------------------------------------------------------------- #

def _load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log("读取 config.json 失败：%s（将继续使用环境变量）" % exc)
        return {}


CONFIG = _load_config()


def getenv(name, default=None):
    value = os.environ.get(name)
    if value is not None and str(value).strip() != "":
        return value
    value = CONFIG.get(name)
    if value is not None and str(value).strip() != "":
        return value
    return default


def parse_id_set(value):
    """把 123 / "123,456" / [123, 456] 统一解析为 {123, 456}。"""
    result = set()
    if value is None:
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = re.split(r"[,\s;，、]+", str(value))
    for item in items:
        text = str(item).strip()
        if re.fullmatch(r"-?\d+", text):
            result.add(int(text))
    return result


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    """把 true/false、1/0、yes/no、开/关 等写法统一成布尔值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "on", "是", "开", "启用"):
        return True
    if text in ("0", "false", "no", "n", "off", "否", "关", "关闭"):
        return False
    return default


TOKEN = getenv("TOKEN")
API_HASH = getenv("HASH")
API_ID_RAW = getenv("ID")

# ---- 用户权限 ----
# 机器人只服务 OWNER_ID 这一个用户，其他人一律拒绝（没有白名单机制）。
# OWNER_ID 就是你自己：启动后给机器人发 /id 即可看到，或找 @userinfobot 查。
_owner_ids = parse_id_set(getenv("OWNER_ID"))
OWNER_ID = min(_owner_ids) if _owner_ids else None

MAX_RANGE = _as_int(getenv("MAX_RANGE", 100), 100)   # 0 表示不限制
DELAY = _as_float(getenv("DELAY", 3), 3.0)           # 每条之间的间隔秒数

# 相册并发下载数（1 = 串行；越大越快，但更吃带宽与 CPU）
ALBUM_WORKERS = max(1, _as_int(getenv("ALBUM_WORKERS", 3), 3))
# 临时文件最长保留秒数 / 定时清理间隔秒数
TEMP_MAX_AGE = _as_int(getenv("TEMP_MAX_AGE", 3600), 3600)
CLEANUP_INTERVAL = _as_int(getenv("CLEANUP_INTERVAL", 1800), 1800)

# 直传策略：auto = 先尝试直接转发，再尝试复制，都不行才下载上传
#         forward = 只尝试转发 / copy = 只尝试复制 / off = 直接下载上传
RELAY_MODE = str(getenv("RELAY_MODE", "auto")).strip().lower()

# 上传前处理：开启后视频/图片会重新编码（改变文件哈希），并统一改名为「原名_时间戳」
TRANSCODE_MODE = _as_bool(getenv("TRANSCODE_MODE", True), True)
VIDEO_CRF = _as_int(getenv("VIDEO_CRF", 24), 24)          # 越小画质越好、文件越大
IMAGE_QUALITY = _as_int(getenv("IMAGE_QUALITY", 92), 92)  # JPEG 质量 1~95
TRANSCODE_TIMEOUT = _as_int(getenv("TRANSCODE_TIMEOUT", 3600), 3600)

# 客户端上报给 Telegram 的设备信息。
# pyrogram 默认值是 "papermaid" / "Pyrogram x.y.z" / "CPython x.y, Linux ..."，
# 这种一眼就假的指纹很容易被风控判为“异常新设备”，进一步拒绝登录。
# 所以这里改成在若干「真实机型画像」里挑一个：
#   DEVICE_PROFILE=auto   默认。按用户 ID / 客户端名做确定性选择，
#                         同一账号永远是同一台设备（推荐）
#   DEVICE_PROFILE=random 每次建连随机挑一个（不建议：同一账号设备会变来变去）
# 也可以显式覆盖任意一项：
#   DEVICE_MODEL / SYSTEM_VERSION / APP_VERSION /
#   LANG_CODE（Telegram 语言包，如 zh-hans）/ SYSTEM_LANG_CODE（系统语言，如 zh-CN）
DEVICE_PROFILE = str(getenv("DEVICE_PROFILE", "auto")).strip().lower()

# (device_model, system_version, app_version, lang_code, system_lang_code)
# 全部取自真实客户端会上报的格式：
#   Telegram Android 的 app_version 形如 “11.2.3 (5274)”，system_version 为 “SDK 34”，
#   lang_code 是语言包代码（zh-hans），system_lang_code 是系统语言（zh-CN）。
DEVICE_PROFILES = [
    ("samsung SM-S928B", "SDK 34", "11.2.3 (5274)", "zh-hans", "zh-CN"),
    ("samsung SM-G998B", "SDK 33", "11.2.3 (5274)", "zh-hans", "zh-CN"),
    ("Xiaomi 23127PN0CC", "SDK 34", "11.2.3 (5274)", "zh-hans", "zh-CN"),
    ("HUAWEI ALN-AL80", "SDK 31", "11.2.3 (5274)", "zh-hans", "zh-CN"),
    ("OPPO PJZ110", "SDK 34", "11.2.3 (5274)", "zh-hans", "zh-CN"),
    ("vivo V2324A", "SDK 34", "11.2.3 (5274)", "zh-hans", "zh-CN"),
    ("Google Pixel 8 Pro", "SDK 34", "11.2.3 (5274)", "en", "en-US"),
    ("samsung SM-A546B", "SDK 33", "11.2.3 (5274)", "en", "en-US"),
]

_DEVICE_OVERRIDE = {
    "device_model": getenv("DEVICE_MODEL", ""),
    "system_version": getenv("SYSTEM_VERSION", ""),
    "app_version": getenv("APP_VERSION", ""),
    "lang_code": getenv("LANG_CODE", ""),
    "system_lang_code": getenv("SYSTEM_LANG_CODE", ""),
}


def pick_device_profile(seed="bot"):
    """挑一个设备画像；auto 模式下同一个 seed 永远得到同一个画像。"""
    if DEVICE_PROFILE == "random" or seed is None:
        return random.choice(DEVICE_PROFILES)
    index = zlib.crc32(str(seed).encode("utf-8")) % len(DEVICE_PROFILES)
    return DEVICE_PROFILES[index]


def device_fingerprint(seed="bot"):
    """生成一组 initConnection 用的设备指纹，显式配置优先。"""
    model, system, app, lang, system_lang = pick_device_profile(seed)
    fp = {
        "device_model": model,
        "system_version": system,
        "app_version": app,
        "lang_code": lang,
        "system_lang_code": system_lang,
    }
    for key, value in _DEVICE_OVERRIDE.items():
        if value:
            fp[key] = value
    return fp


def apply_fingerprint(client, seed="bot"):
    """把设备指纹写回客户端（pyrogram 在建立连接时才读取这些属性）。"""
    fp = device_fingerprint(seed)
    client.device_model = fp["device_model"]
    client.system_version = fp["system_version"]
    client.app_version = fp["app_version"]
    client.lang_code = str(fp["lang_code"]).lower()
    client.system_lang_code = fp["system_lang_code"]
    return client


def parse_proxy(value):
    """
    解析代理，支持：
        socks5://user:pass@host:port
        socks5://host:port
        http://host:port
    """
    if not value:
        return None
    try:
        from urllib.parse import unquote, urlparse

        text = str(value).strip()
        parsed = urlparse(text if "://" in text else "socks5://" + text)
        scheme = (parsed.scheme or "socks5").lower()
        if scheme not in ("socks4", "socks5", "http"):
            log("不支持的代理协议：%s（可选 socks4 / socks5 / http）" % scheme)
            return None
        if not parsed.hostname or not parsed.port:
            log("代理格式不正确，应为 协议://[用户名:密码@]主机:端口")
            return None
        proxy = {"scheme": scheme, "hostname": parsed.hostname, "port": parsed.port}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy
    except Exception as exc:  # noqa: BLE001
        log("解析代理失败：%s" % exc)
        return None


PROXY = parse_proxy(getenv("PROXY"))
# 所有客户端共用的额外参数（目前只有代理）
CLIENT_EXTRA = {"proxy": PROXY} if PROXY else {}

if not TOKEN or not API_HASH or not API_ID_RAW:
    log("缺少必要配置：TOKEN / HASH / ID，请检查环境变量或 config.json")
    sys.exit(1)

try:
    API_ID = int(API_ID_RAW)
except ValueError:
    log("ID 必须是纯数字（my.telegram.org 的 api_id）")
    sys.exit(1)

# --------------------------------------------------------------------------- #
# 2. 机器人客户端
# --------------------------------------------------------------------------- #

bot = apply_fingerprint(Client(
    "mybot",
    api_id=API_ID,
    api_hash=str(API_HASH),
    bot_token=str(TOKEN),
    workdir=BASE_DIR,
    **CLIENT_EXTRA
), "bot")


async def _new_client(seed=None, **kwargs):
    """在核心事件循环里创建并启动一个客户端。"""
    kwargs.update(CLIENT_EXTRA)
    client = apply_fingerprint(
        Client(api_id=API_ID, api_hash=str(API_HASH), workdir=BASE_DIR, **kwargs),
        seed)
    await client.start()
    return client


async def _new_login_client(name, seed=None):
    """创建用于登录、尚未授权的客户端。"""
    client = apply_fingerprint(Client(
        name,
        api_id=API_ID,
        api_hash=str(API_HASH),
        workdir=BASE_DIR,
        in_memory=True,
        **CLIENT_EXTRA
    ), seed)
    await client.connect()
    return client


def run_on_loop(coroutine, timeout=120):
    """
    在机器人的核心事件循环里执行协程。

    pyrogram 的 Client 只能在创建 bot 的那个事件循环里构造，
    而命令处理函数运行在线程池里，所以这里显式切回主循环。
    带上超时，避免网络断了时整个线程被永久卡住。
    """
    loop = getattr(bot, "loop", None)
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            log("[警告] 事件循环操作超时（%s 秒），已取消" % timeout)
            raise
    return asyncio.get_event_loop().run_until_complete(coroutine)


# --------------------------------------------------------------------------- #
# 3. 用户会话：每用户自助登录 + 持久化
# --------------------------------------------------------------------------- #

# pyrogram 的会话字符串格式：
#   新格式 ">BI?256sQ?" -> 271 字节；旧格式 ">B?256sI?" 263 字节 / 带 64 位 userId 267 字节
_SESSION_PACKS = {
    271: ">BI?256sQ?",
    263: ">B?256sI?",
    267: ">B?256sQ?",
}


def is_valid_session_string(text):
    """
    粗校验 session string 是否能被 pyrogram 正确解析。

    不校验的话，sessions.json 里存了被截断 / 粘贴不全的字符串时，
    pyrogram 只会抛一个难懂的 “unpack requires a buffer of 271 bytes”，
    用户完全不知道发生了什么。
    """
    if not isinstance(text, str) or not text.strip():
        return False
    text = text.strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        pack = _SESSION_PACKS.get(len(raw))
        if pack is None:
            return False
        struct.unpack(pack, raw)
        return True
    except Exception:  # noqa: BLE001
        return False


def load_sessions():
    """读取 sessions.json：{用户ID: session_string}，自动丢弃损坏的条目。"""
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as exc:  # noqa: BLE001
        log("读取 sessions.json 失败：%s" % exc)
        return {}

    result = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if not (str(key).strip().isdigit()
                    and isinstance(value, str) and value.strip()):
                continue
            user_id = int(str(key).strip())
            if is_valid_session_string(value):
                result[user_id] = value.strip()
            else:
                log("[警告] 用户 %s 的会话字符串已损坏，已忽略，请重新 /login"
                    % user_id)
    return result


def save_sessions(data):
    """原子写入 sessions.json（权限 600，里面是会话凭证）。"""
    try:
        tmp = SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({str(k): v for k, v in data.items()},
                      fp, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_FILE)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        log("写入 sessions.json 失败：%s" % exc)


SESSIONS = load_sessions()            # {user_id: session_string}
USER_CLIENTS = {}                     # {user_id: Client}
USER_CLIENTS_LOCK = threading.Lock()
LOGIN_STATE = {}                      # {user_id: {...}} 登录中的临时状态

BOT_ID = None                         # 机器人自己的 ID（启动后填充）


def get_user_client(user_id):
    """取该用户已登录的客户端；不存在或不可用时返回 None。"""
    if not user_id:
        return None
    client = USER_CLIENTS.get(user_id)
    if client is not None:
        return client
    session = SESSIONS.get(user_id)
    if not session:
        return None
    with USER_CLIENTS_LOCK:
        client = USER_CLIENTS.get(user_id)
        if client is not None:
            return client
        try:
            client = run_on_loop(_new_client(
                seed="user_%s" % user_id,
                name="user_%s" % user_id,
                session_string=session,
                in_memory=True,
            ))
        except Exception as exc:  # noqa: BLE001
            log("用户 %s 的会话恢复失败：%s: %s"
                % (user_id, type(exc).__name__, exc))
            # 会话已不可用（格式坏了 / 已失效 / 被踢下线），清掉免得反复报错
            if SESSIONS.pop(user_id, None) is not None:
                save_sessions(SESSIONS)
                log("已删除用户 %s 失效的会话，需要重新 /login" % user_id)
            return None
        USER_CLIENTS[user_id] = client
        log("用户 %s 的会话已恢复" % user_id)
        return client


def resolve_session(message, hint=""):
    """
    取这条消息要用的用户会话：必须是请求者自己 /login 的会话。
    没有则回复提示并返回 None。

    hint：额外说明为什么需要会话（例如公开链接机器人复制失败的原因）。
    """
    user = message.from_user
    user_id = user.id if user else None

    if user_id and SESSIONS.get(user_id):
        client = get_user_client(user_id)
        if client is not None:
            return client
        reply(message, "❌ 你保存的会话已失效（可能已在手机上退出登录），请发送 /login 重新登录。")
        return None

    reply(message, no_session_text(hint))
    return None


def discard_login(user_id):
    """清理未完成的登录状态（含扫码线程）。"""
    state = LOGIN_STATE.pop(user_id, None)
    if not state:
        return
    stop = state.get("stop")
    if stop is not None:
        try:
            stop.set()
        except Exception:  # noqa: BLE001
            pass
    _drop_qr(state)
    client = state.get("client")
    if client is not None:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _notify(chat_id, text):
    """向指定会话发送消息（可在任意线程调用，失败只记日志）。"""
    if not chat_id:
        return None
    try:
        return bot.send_message(chat_id, text, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        log("向 %s 发送消息失败：%s" % (chat_id, exc))
        return None


def finish_login(message, client):
    """登录成功：导出会话字符串并持久化。"""
    return finish_login_for(message.from_user.id, message.chat.id, client, message)


def finish_login_for(user_id, chat_id, client, message=None):
    """登录成功后的收尾：导出会话字符串、落盘、写回内存。"""
    try:
        session_string = client.export_session_string()
        me = client.get_me()
    except Exception as exc:  # noqa: BLE001
        log("用户 %s 导出会话失败：%s: %s" % (user_id, type(exc).__name__, exc))
        traceback.print_exc()
        if message is not None:
            reply(message, error_text(exc))
        else:
            _notify(chat_id, error_text(exc))
        discard_login(user_id)
        return False

    SESSIONS[user_id] = session_string
    save_sessions(SESSIONS)
    USER_CLIENTS[user_id] = client

    # 登录已完成，只移除状态，不能走 discard_login（那会断开刚登录的客户端）
    state = LOGIN_STATE.pop(user_id, None)
    if state is not None:
        stop = state.get("stop")
        if stop is not None:
            stop.set()
        _drop_qr(state)

    name = " ".join(filter(None, [getattr(me, "first_name", None),
                                  getattr(me, "last_name", None)]))
    if not name:
        name = getattr(me, "username", None) or str(getattr(me, "id", ""))

    log("用户 %s 登录成功（账号 %s）" % (user_id, getattr(me, "id", "?")))
    out = ("✅ 登录成功：**%s**\n\n"
           "你的会话已保存，现在可以直接发送私有 / 受限内容链接了。\n"
           "如需解绑请发送 /logout。" % name)
    if message is not None:
        reply(message, out)
    else:
        _notify(chat_id, out)
    return True


# --------------------------------------------------------------------------- #
# 3.5 扫码登录（QR Login）
# --------------------------------------------------------------------------- #

async def _export_login_token(client):
    """向 Telegram 申请一个登录用 token（二维码内容）。"""
    return await client.invoke(
        raw.functions.auth.ExportLoginToken(
            api_id=API_ID,
            api_hash=str(API_HASH),
            except_ids=[],
        )
    )


async def _accept_qr_authorization(client, authorization):
    """扫码确认后，把授权信息写回客户端存储。"""
    user = getattr(authorization, "user", None)
    user_id = getattr(user, "id", None)
    if user_id:
        await client.storage.user_id(user_id)
        await client.storage.is_bot(False)
    return user_id


def qr_png_path(data, name):
    """把 tg://login?token=... 渲染成 PNG 文件，返回路径。"""
    if not QR_AVAILABLE:
        return None
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        path = os.path.join(DOWNLOAD_DIR, "%s.png" % name)
        _qrcode.make(data).save(path)
        return path
    except Exception as exc:  # noqa: BLE001
        log("生成二维码失败：%s" % exc)
        return None


def _drop_qr(state):
    """删除已发送的二维码消息与临时图片。"""
    msg_id = state.pop("qr_message_id", None)
    chat_id = state.get("chat_id")
    if msg_id and chat_id:
        try:
            bot.delete_messages(chat_id, [msg_id])
        except Exception:  # noqa: BLE001
            pass
    safe_remove(state.pop("qr_path", None))


def _show_qr(state, chat_id, url):
    """发送 / 刷新二维码图片（仅在 token 变化时调用）。"""
    caption = "请用手机 Telegram 扫描二维码登录\n发送 /cancel 可取消"
    path = qr_png_path(url, "qr_%s_%s" % (chat_id, uuid.uuid4().hex[:8]))
    if path is None:
        _notify(chat_id,
                "服务器缺少 qrcode 库，无法生成二维码图片。\n"
                "请在手机上打开下面的链接登录：\n`%s`" % url)
        return

    old_path = state.get("qr_path")
    msg_id = state.get("qr_message_id")
    if msg_id:
        try:
            bot.edit_message_media(chat_id, msg_id,
                                   InputMediaPhoto(path, caption=caption))
            state["qr_path"] = path
            safe_remove(old_path)
            return
        except Exception as exc:  # noqa: BLE001
            if "MESSAGE_NOT_MODIFIED" in str(exc):
                # 内容完全一致，属于正常情况，直接当成已刷新
                state["qr_path"] = path
                safe_remove(old_path)
                return
            log("刷新二维码失败，改为重新发送：%s" % exc)
            _drop_qr(state)

    try:
        sent = bot.send_photo(chat_id, path, caption=caption)
        state["qr_message_id"] = sent.id
        state["qr_path"] = path
    except Exception as exc:  # noqa: BLE001
        log("发送二维码失败：%s" % exc)
        safe_remove(path)
        _notify(chat_id, "发送二维码失败：%s\n可以改用手机号登录：/login phone" % exc)


def start_qr_login(user_id, message):
    """生成二维码并启动后台线程等待扫码确认。"""
    chat_id = message.chat.id

    if not QR_AVAILABLE:
        reply(message,
              "服务器缺少 qrcode 库，无法生成二维码。\n"
              "请执行 `pip install qrcode` 后重启，或改用手机号登录：`/login phone`")
        return

    try:
        login_client = run_on_loop(_new_login_client(
            "login_%s" % user_id, seed="user_%s" % user_id))
    except Exception as exc:  # noqa: BLE001
        log("用户 %s 创建扫码客户端失败：%s: %s"
            % (user_id, type(exc).__name__, exc))
        reply(message, error_text(exc))
        return

    state = {
        "step": "qr",
        "client": login_client,
        "started_at": time.time(),
        "stop": threading.Event(),
        "chat_id": chat_id,
        "qr_message_id": None,
        "qr_path": None,
        "qr_url": None,
    }
    LOGIN_STATE[user_id] = state

    log("用户 %s 开始扫码登录" % user_id)

    reply(message,
          "**扫码登录**\n\n"
          "手机 Telegram → 设置 → 设备 → 连接设备 → 扫描下面的二维码。\n"
          "二维码过期后会自动刷新，请在 %d 分钟内完成。\n"
          "发送 /cancel 可取消。" % (QR_LOGIN_TIMEOUT // 60))

    threading.Thread(target=_qr_login_worker,
                     args=(user_id, state),
                     name="qrlogin-%s" % user_id,
                     daemon=True).start()


def _qr_login_worker(user_id, state):
    """扫码线程入口：任何未预期异常都要记日志，不能静默退出。"""
    try:
        _qr_login_loop(user_id, state)
    except BaseException as exc:  # noqa: BLE001
        log("用户 %s 扫码线程异常退出：%s: %s" % (user_id, type(exc).__name__, exc))
        traceback.print_exc()
        try:
            _drop_qr(state)
            discard_login(user_id)
            _notify(state.get("chat_id"),
                    "扫码登录出现异常：`%s: %s`\n可以改用手机号登录：/login phone"
                    % (type(exc).__name__, exc))
        except Exception:  # noqa: BLE001
            pass


async def _qr_migrate_and_import(client, dc_id, token):
    """
    账号不在当前 DC 时，把未授权的登录客户端切到目标 DC，
    再用 auth.importLoginToken 把迁移 token 换成真正的登录结果。

    Telegram 对“账号在别的数据中心”的情况，让 exportLoginToken 返回
    auth.loginTokenMigrateTo(dc_id, token)，必须去那个 DC 上 import 完才能继续，
    否则拿到的只是个“迁移 token”，扫了也不会生效。
    流程与 pyrogram 自带的 send_code 迁移分支保持一致。
    """
    from pyrogram.session import Auth, Session

    old_session = getattr(client, "session", None)
    if old_session is not None:
        try:
            await old_session.stop()
        except Exception:  # noqa: BLE001
            pass

    await client.storage.dc_id(dc_id)
    await client.storage.auth_key(
        await Auth(client, dc_id, await client.storage.test_mode()).create())

    client.session = Session(client, dc_id, await client.storage.auth_key(),
                             await client.storage.test_mode())
    await client.session.start()

    return await client.invoke(
        raw.functions.auth.ImportLoginToken(token=token))


def _qr_login_loop(user_id, state):
    """轮询登录 token：刷新二维码、处理扫码结果、DC 迁移与两步验证。"""
    client = state.get("client")
    stop = state.get("stop")
    chat_id = state.get("chat_id")
    deadline = time.time() + QR_LOGIN_TIMEOUT

    if client is None or stop is None:
        log("用户 %s 扫码登录状态不完整，放弃" % user_id)
        return

    polls = 0
    while not stop.is_set() and time.time() < deadline:
        polls += 1
        try:
            result = run_on_loop(_export_login_token(client))
            # 账号在其他 DC：先迁过去 import，才能拿到真正的登录结果
            if isinstance(result, raw.types.auth.LoginTokenMigrateTo):
                log("用户 %s 的账号在 DC%s，迁移登录客户端后导入 token"
                    % (user_id, result.dc_id))
                if not state.get("dc_notice"):
                    state["dc_notice"] = True
                    _notify(chat_id,
                            "该账号位于 **DC%s**，机器人正在切换数据中心，"
                            "二维码稍后自动刷新，请稍等几秒再扫。" % result.dc_id)
                result = run_on_loop(
                    _qr_migrate_and_import(client, result.dc_id, result.token))
        except SessionPasswordNeeded:
            # 已扫码并确认，但账号开了两步验证
            log("用户 %s 扫码已确认，需要两步验证密码" % user_id)
            _drop_qr(state)
            state.update(step="password", started_at=time.time())
            _notify(chat_id,
                    "✅ 扫码成功！\n\n"
                    "该账号开启了两步验证，请发送**两步验证密码**。\n"
                    "发送 /cancel 可取消。")
            return
        except FloodWait as exc:
            seconds = getattr(exc, "value", 10) or 10
            log("扫码登录触发频率限制，等待 %s 秒" % seconds)
            stop.wait(min(int(seconds), 60))
            continue
        except Exception as exc:  # noqa: BLE001
            log("用户 %s 扫码登录出错（第 %d 次轮询）：%s: %s"
                % (user_id, polls, type(exc).__name__, exc))
            if not stop.is_set():
                _drop_qr(state)
                discard_login(user_id)
                _notify(chat_id, error_text(exc))
            return

        if isinstance(result, raw.types.auth.LoginTokenSuccess):
            log("用户 %s 扫码登录已确认，正在导出会话" % user_id)
            run_on_loop(_accept_qr_authorization(client, result.authorization))
            stop.set()
            finish_login_for(user_id, chat_id, client)
            return

        if not isinstance(result, raw.types.auth.LoginToken):
            log("用户 %s 扫码登录返回了预期之外的类型：%s"
                % (user_id, type(result).__name__))
            stop.wait(QR_POLL_SECONDS)
            continue

        url = "tg://login?token=" + base64.urlsafe_b64encode(
            result.token).decode().rstrip("=")
        # token 未过期时 Telegram 会返回同一个值，此时不要重画二维码，
        # 否则同一张图重复 edit 会报 MESSAGE_NOT_MODIFIED
        if url != state.get("qr_url"):
            log("用户 %s 二维码已就绪（第 %d 次轮询）" % (user_id, polls))
            _show_qr(state, chat_id, url)
            state["qr_url"] = url

        if polls % 10 == 0:
            left = int(deadline - time.time())
            log("用户 %s 扫码登录等待中（已轮询 %d 次，剩余 %d 秒）"
                % (user_id, polls, max(left, 0)))
        stop.wait(QR_POLL_SECONDS)

    if stop.is_set():
        log("用户 %s 扫码登录已取消" % user_id)
        return
    log("用户 %s 扫码登录超时" % user_id)
    _drop_qr(state)
    discard_login(user_id)
    _notify(chat_id, "二维码已过期，请重新发送 /login 获取新的二维码。")


# --------------------------------------------------------------------------- #
# 4. 提示文案
# --------------------------------------------------------------------------- #

NO_SESSION_TEXT = (
    "❌ **这条内容需要登录你自己的账号**\n\n"
    "公开频道的内容机器人会直接转发，**不需要登录**；\n"
    "只有**私有群组 / 受限内容**，以及**机器人不在其中的会话**，才需要用你自己的账号读取。\n\n"
    "发送 /login 绑定账号（只需一次，之后一直有效），然后重新发送这条链接。"
)


def no_session_text(hint=""):
    """按场景生成「需要登录」的提示。"""
    if not hint:
        return NO_SESSION_TEXT
    return ("❌ **这条内容需要登录你自己的账号**\n\n"
            "%s\n\n"
            "发送 /login 绑定账号（只需一次）后可下载；"
            "公开频道的内容不需要登录。" % hint)

LOGIN_PROMPT_PHONE = (
    "**手机号登录**\n\n"
    "请发送手机号（含国家码），例如：`+8613800138000`\n\n"
    "- 该号码仅用于生成会话，会话保存在本机器人服务器上\n"
    "- 建议使用小号，账号权限等同于你在手机上登录\n"
    "- 发送 /cancel 可随时取消"
)

LOGIN_CHOICE_TEXT = (
    "**登录你自己的 Telegram 账号**\n\n"
    "请选择登录方式：\n"
    "- **手机号登录**：发送手机号 → 验证码 →（如有）两步验证密码\n"
    "- **扫码登录**：用手机 Telegram 扫描二维码，无需输入验证码\n\n"
    "会话仅保存在本机器人服务器上，建议使用小号。\n"
    "想更换账号请先发送 /logout。\n\n"
    "如果手机号登录总提示验证码失效（ Telegram 风控），直接用\n"
    "`/login <会话字符串>` 绑定现成的 session string。"
)


def login_choice_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("手机号登录", callback_data="login:phone"),
        InlineKeyboardButton("扫码登录", callback_data="login:qr"),
    ]])

LOGIN_BLOCKED_HINT = (
    "提示：如果 Telegram 同时给你发了「Incomplete login attempt … this code was "
    "previously shared」的通知，通常是因为**验证码消息被转发了**，"
    "不要转发、不要截图，手动输入数字。"
)

LOGIN_BLOCKED_TEXT = (
    "❌ 这次登录被 Telegram 风控拦截了。\n\n"
    "你收到的「Incomplete login attempt / The code was entered correctly, but sign in "
    "was not allowed, because this code was previously shared by your account」，"
    "意思很明确：**验证码本身是对的，但 Telegram 判定这个验证码“被分享过”**。\n\n"
    "最常见的原因：\n"
    "1. **把 Telegram 的「Login code」消息转发到别处**（包括转发给这个机器人）\n"
    "   —— 一转发 Telegram 就会把该验证码作废，且这次登录直接拒绝\n"
    "2. 把验证码粘到了网页 / 其它软件 / 发给了别人\n"
    "3. 同一个验证码先在其他客户端试过\n\n"
    "正确做法：\n"
    "- 不要转发，也不要截图，**自己看数字后手动输入**给机器人；\n"
    "- 删掉之前转发的那些消息，避免 Telegram 继续把新码也标记为已分享；\n"
    "- 等 10~30 分钟再 /login（不要连着重试，越试风控越严）；\n"
    "- 如果一直不行，等几小时后在官方客户端正常登录一次，或改用 `/login <会话字符串>`。\n\n"
    "机器人跑在机房 IP 时风控会更严，可配住宅代理（PROXY）。"
)

# 验证码消息被转发时的提醒
FORWARDED_CODE_TEXT = (
    "⚠️ **检测到这是转发过来的消息**\n\n"
    "Telegram 的「Login code」消息一旦被转发，验证码就会被标记为**已分享**，"
    "之后无论怎么输都会报失败（就是那条 Incomplete login attempt 通知）。\n\n"
    "请：\n"
    "1. 删掉刚才转发的消息；\n"
    "2. 隔 10~30 分钟再 /login；\n"
    "3. 拿到新验证码后**自己看数字、手动打出来**发给我，不要转发。"
)

HELP_TEXT = """**使用说明**

**1. 登录（按需，公开内容不用登录）**
/login 选择登录方式：手机号登录 / 扫码登录
/login qr 直接生成登录二维码
/login <会话字符串> 直接使用已有的 session string 登录
/logout 解绑并删除已保存的会话
/id 查看自己的用户 ID

手机号登录若总报验证码失效（Telegram 风控），请改用扫码登录，
或本地生成 session string 后发 `/login <字符串>`。

私有群组 / 受限内容 / 机器人不在其中的会话才需要登录。

**2. 直接发送链接**
- 公开：`https://t.me/频道用户名/123`
- 私有：`https://t.me/c/123456/1234`
- 话题群：`https://t.me/c/123456/1/12345?single`
- 评论区：`https://t.me/频道用户名/123?comment=456`（下载评论本身）
- 机器人：`https://t.me/b/机器人用户名/4321`
- 批量：`https://t.me/频道用户名/1001-1010`

**3. 相册 / 多文件**
发相册中任意一条消息的链接，整组一起发送；带 `?single` 只发这一条。

**4. 邀请链接**
发送 **邀请链接** 可让你的会话加入该群组。

**5. 更多帮助**
https://blog.lhl.one/artical/1365.html
"""


# 注册到 Telegram 指令菜单的命令列表（启动时自动同步）
BOT_COMMANDS = [
    BotCommand("start", "开始使用 / 查看说明"),
    BotCommand("help", "查看所有支持的链接格式"),
    BotCommand("login", "登录账号：手机号 / 扫码 / session 字符串"),
    BotCommand("logout", "解绑并删除已保存的会话"),
    BotCommand("id", "查看自己的用户 ID"),
    BotCommand("cancel", "取消进行中的登录"),
]


def _keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Github 仓库", url=FORK_REPO),
    ]])


# --------------------------------------------------------------------------- #
# 5. 通用工具
# --------------------------------------------------------------------------- #

def is_allowed(user_id):
    """权限判断：只有 OWNER_ID 本人可以使用。"""
    if OWNER_ID is None or user_id is None:
        return False
    try:
        return int(user_id) == int(OWNER_ID)
    except (TypeError, ValueError):
        return False


def is_authorized(message):
    user = message.from_user
    if user is None:
        return True
    return is_allowed(user.id)


def reply(message, text, **kwargs):
    """回复消息；如果原消息已删除则退化为普通发送。"""
    kwargs.setdefault("disable_web_page_preview", True)
    try:
        return bot.send_message(
            message.chat.id, text, reply_to_message_id=message.id, **kwargs)
    except Exception:  # noqa: BLE001
        try:
            return bot.send_message(message.chat.id, text, **kwargs)
        except Exception:  # noqa: BLE001
            return None


ERROR_HINTS = {
    "UserAlreadyParticipant": "该账号已经在这个群组/频道中",
    "InviteHashExpired": "邀请链接已失效",
    "InviteHashInvalid": "邀请链接无效",
    "UsernameNotOccupied": "该用户名不存在",
    "UsernameInvalid": "用户名格式不正确",
    "ChannelPrivate": "该频道/群组是私有的，会话账号不在其中或已被限制",
    "ChannelInvalid": "无法访问该频道/群组，请确认链接或先加入",
    "ChatAdminRequired": "需要管理员权限才能执行该操作",
    "MessageIdInvalid": "消息 ID 无效或消息已被删除",
    "MessageEmpty": "该消息为空（可能已被删除）",
    "MediaEmpty": "该消息不包含可下载的媒体",
    "UserNotParticipant": "会话账号不在该群组中",
    "FileReferenceExpired": "文件引用已过期，请重新发送链接",
    "AuthKeyUnregistered": "会话无效或已失效，请发送 /login 重新登录",
    "SessionRevoked": "会话已被注销，请发送 /login 重新登录",
    "SessionPasswordNeeded": "该账号开启了两步验证",
    "ChatWriteForbidden": "机器人没有在该会话发言的权限",
    "SlowmodeWait": "目标会话开启了慢速模式",
    "PhoneNumberInvalid": "手机号格式不正确（需带国家码，例如 +8613800138000）",
    "PhoneNumberBanned": "该手机号已被 Telegram 封禁",
    "PhoneCodeInvalid": "验证码不正确",
    "PhoneCodeEmpty": "验证码不能为空",
    "PhoneCodeExpired": "验证码已过期，请重新发送 /login",
    "PasswordHashInvalid": "两步验证密码不正确",
}


def error_text(error):
    """把 pyrogram 异常翻译成中文提示。"""
    if isinstance(error, FloodWait):
        seconds = getattr(error, "value", None) or getattr(error, "x", None) or "若干"
        return "触发 Telegram 频率限制，请 **%s 秒**后再试。" % seconds
    if is_peer_error(error):
        return peer_error_text(error)
    name = type(error).__name__
    hint = ERROR_HINTS.get(name)
    if hint:
        return "❌ 处理失败：%s\n`%s: %s`" % (hint, name, error)
    return "❌ 处理失败：`%s: %s`" % (name, error)


def get_message_type(msg):
    """判断消息类型。"""
    for attr, name in (
        ("document", "Document"),
        ("video", "Video"),
        ("animation", "Animation"),
        ("sticker", "Sticker"),
        ("voice", "Voice"),
        ("audio", "Audio"),
        ("photo", "Photo"),
    ):
        if getattr(msg, attr, None):
            return name
    if getattr(msg, "text", None):
        return "Text"
    return None


def format_size(num):
    """把字节数格式化成可读文本。"""
    if not num:
        return "0 B"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.2f %s" % (value, unit)
        value /= 1024.0
    return "%.2f TB" % value


class SpeedMeter:
    """简单的速度采样器（带平滑）。"""

    def __init__(self):
        self.last_time = 0.0
        self.last_size = 0
        self.speed = 0.0

    def update(self, current):
        now = time.time()
        if not self.last_time:
            self.last_time, self.last_size = now, current
            return 0.0
        elapsed = now - self.last_time
        if elapsed < 0.2:
            return self.speed
        instant = max(0, current - self.last_size) / elapsed
        self.speed = instant if not self.speed else self.speed * 0.65 + instant * 0.35
        self.last_time, self.last_size = now, current
        return self.speed


def make_progress(status_message, label, prefix="", interval=4.0,
                  cancel_event=None, gate=None, markup=None):
    """
    生成上传/下载进度回调：显示实时大小、百分比与速度，并支持取消。

    形如：__已下载__：**[2/5] 12.34 MB / 45.67 MB（27.0%）**｜3.21 MB/s

    cancel_event：threading.Event，一旦被 set，进度回调会抛 StopTransmission 中止传输。
    gate：可共享的节流容器（相册并发下载时共用一个，避免多条进度互相刷屏）。
    """
    gate = gate if gate is not None else {"last_edit": 0.0}
    meter = SpeedMeter()
    if prefix and not prefix.endswith(" "):
        prefix += " "

    def _progress(current, total):
        if cancel_event is not None and cancel_event.is_set():
            raise StopTransmission()

        speed = meter.update(current)
        now = time.time()
        finished = bool(total) and current >= total
        if not finished and now - gate["last_edit"] < interval:
            return
        gate["last_edit"] = now

        if total:
            detail = "%s / %s（%.1f%%）" % (
                format_size(current), format_size(total), current * 100.0 / total)
        else:
            detail = format_size(current)

        text = "__%s__：**%s%s**" % (label, prefix, detail)
        if speed > 0 and not finished:
            text += "｜%s/s" % format_size(speed)

        try:
            # 注意：Bot API 的 editMessageText 在未提供 reply_markup 时会
            # 把已有的内联键盘删掉，所以这里必须每次都带上取消按钮。
            bot.edit_message_text(status_message.chat.id, status_message.id, text,
                                  reply_markup=markup)
        except Exception:  # noqa: BLE001
            pass

    return _progress


# --------------------------------------------------------------------------- #
# 5.2 任务取消（下载/上传进度条上的「取消」按钮）
# --------------------------------------------------------------------------- #

ACTIVE_TASKS = {}          # {token: {"event": Event, "chat_id": int, "message_id": int}}
TASKS_LOCK = threading.Lock()


def cancel_markup(token):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("取消", callback_data="cancel:%s" % token),
    ]])


def create_cancel_task(message, text):
    """发一条带「取消」按钮的状态消息，返回 (status, token, cancel_event)。"""
    token = uuid.uuid4().hex[:10]
    event = threading.Event()
    try:
        status = bot.send_message(
            message.chat.id, text,
            reply_to_message_id=message.id,
            reply_markup=cancel_markup(token))
    except Exception:  # noqa: BLE001
        status = bot.send_message(message.chat.id, text,
                                  reply_markup=cancel_markup(token))

    with TASKS_LOCK:
        ACTIVE_TASKS[token] = {"event": event,
                               "chat_id": status.chat.id,
                               "message_id": status.id}
    return status, token, event


def finish_cancel_task(token):
    with TASKS_LOCK:
        ACTIVE_TASKS.pop(token, None)


@bot.on_callback_query(filters.regex(r"^cancel:"))
def cb_cancel(client, query):
    """点击「取消」：通知传输循环抛 StopTransmission 中止，并清理临时文件。"""
    token = (query.data or "").split(":", 1)[-1]
    with TASKS_LOCK:
        task = ACTIVE_TASKS.get(token)

    if task is None:
        try:
            query.answer("任务已经结束")
        except Exception:  # noqa: BLE001
            pass
        return

    task["event"].set()
    try:
        query.answer("正在取消…")
    except Exception:  # noqa: BLE001
        pass
    try:
        bot.edit_message_text(task["chat_id"], task["message_id"], "__取消中…__")
    except Exception:  # noqa: BLE001
        pass


def download_thumb(client, msg):
    """尽量下载一个缩略图，失败返回 None。"""
    for attr in ("document", "video", "audio", "animation"):
        obj = getattr(msg, attr, None)
        thumbs = getattr(obj, "thumbs", None) if obj else None
        if not thumbs:
            continue
        for thumb in reversed(thumbs):
            try:
                return client.download_media(
                    thumb.file_id,
                    file_name=os.path.join(
                        DOWNLOAD_DIR, "thumb_%s_%s" % (msg.chat.id, msg.id)),
                )
            except Exception:  # noqa: BLE001
                continue
    return None


def safe_remove(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 5.3 上传前处理：改名 + 重新编码（改变文件哈希）
# --------------------------------------------------------------------------- #

_TOOL_CACHE = {}


def ffmpeg_bin():
    """返回 ffmpeg 路径；只提示一次。"""
    if "ffmpeg" not in _TOOL_CACHE:
        _TOOL_CACHE["ffmpeg"] = shutil.which("ffmpeg")
        if not _TOOL_CACHE["ffmpeg"]:
            log("[警告] 未找到 ffmpeg，视频只能改名后原样上传（哈希不会变）")
            log("        安装命令：apt install -y ffmpeg（或 yum install ffmpeg）")
    return _TOOL_CACHE["ffmpeg"]


def _safe_stem(path):
    """从路径取出适合做文件名的部分。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"[^\w\-.]", "_", stem).strip("._") or "file"
    return stem[:80]


def _run_ffmpeg(cmd):
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=TRANSCODE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        log("ffmpeg 调用失败：%s" % exc)
        return False
    if result.returncode != 0:
        tail = (result.stdout or b"").decode("utf-8", "ignore")[-200:]
        log("ffmpeg 转码失败：%s" % tail.strip())
        return False
    return True


def ffmpeg_has(encoder):
    """判断 ffmpeg 是否带某个编码器（结果缓存）。"""
    if "encoders" not in _TOOL_CACHE:
        text = ""
        ffmpeg = ffmpeg_bin()
        if ffmpeg:
            try:
                result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        timeout=30)
                text = (result.stdout or b"").decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                text = ""
        _TOOL_CACHE["encoders"] = text
    return (" %s " % encoder) in _TOOL_CACHE["encoders"]


def transcode_video(src, dst):
    """
    重新编码视频并清除元数据，确保文件哈希变化。

    编码器按可用性回退：libx264 -> libopenh264 -> mpeg4；
    都不行时退化为只重封装（-c copy），仍能改掉容器元数据。
    """
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return False

    audio_args = (["-c:a", "aac", "-b:a", "128k"]
                  if ffmpeg_has("aac") else ["-c:a", "copy"])
    head = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", src,
            "-map_metadata", "-1", "-pix_fmt", "yuv420p"]
    tail = audio_args + ["-movflags", "+faststart", dst]

    candidates = []
    if ffmpeg_has("libx264"):
        candidates.append(head + ["-c:v", "libx264", "-preset", "veryfast",
                                  "-crf", str(VIDEO_CRF)] + tail)
    if ffmpeg_has("libopenh264"):
        candidates.append(head + ["-c:v", "libopenh264", "-b:v", "1500k"] + tail)
    if ffmpeg_has("mpeg4"):
        candidates.append(head + ["-c:v", "mpeg4", "-q:v", "4"] + tail)
    # 兜底：只重封装（不重新编码，速度最快，但改哈希能力有限）
    candidates.append([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                       "-i", src, "-map_metadata", "-1",
                       "-c", "copy", dst])

    for cmd in candidates:
        if _run_ffmpeg(cmd) and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return True
        safe_remove(dst)
    return False


def transcode_image_ffmpeg(src, dst):
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", src, "-q:v", "2", dst]
    return _run_ffmpeg(cmd) and os.path.exists(dst)


def transcode_image(src, dst):
    """优先用 Pillow 重存 JPEG（顺带抹掉 EXIF），没有 Pillow 时用 ffmpeg。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return transcode_image_ffmpeg(src, dst)

    try:
        with Image.open(src) as image:
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(dst, "JPEG", quality=IMAGE_QUALITY, optimize=True,
                       progressive=True)
        return os.path.exists(dst)
    except Exception as exc:  # noqa: BLE001
        log("Pillow 重新编码失败（%s），改用 ffmpeg" % exc)
        return transcode_image_ffmpeg(src, dst)


def prepare_upload_file(path, msg_type):
    """
    上传前处理：
      1. 文件重命名为「原名_年月日-时分秒.扩展名」；
      2. 视频 / 图片会重新编码（清元数据、调参数），彻底改变文件哈希。

    返回实际上传用的路径（处理失败时退化为仅改名，再失败就返回原路径）。
    """
    if not path or not os.path.exists(path):
        return path

    ext = os.path.splitext(path)[1].lower()
    stem = _safe_stem(path)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    target_ext = ext
    if TRANSCODE_MODE:
        if msg_type in ("Video", "Animation"):
            target_ext = ".mp4"
        elif msg_type == "Photo":
            target_ext = ".jpg"

    new_path = os.path.join(DOWNLOAD_DIR, "%s_%s%s" % (stem, stamp, target_ext))

    if TRANSCODE_MODE:
        ok = False
        if msg_type in ("Video", "Animation"):
            ok = transcode_video(path, new_path)
        elif msg_type == "Photo":
            ok = transcode_image(path, new_path)
        if ok:
            safe_remove(path)
            return new_path

    # 仅改名（或转码失败时的兜底）
    fallback = os.path.join(DOWNLOAD_DIR, "%s_%s%s" % (stem, stamp, ext))
    try:
        if fallback != path:
            os.replace(path, fallback)
        return fallback
    except Exception as exc:  # noqa: BLE001
        log("重命名上传文件失败：%s" % exc)
        return path


# --------------------------------------------------------------------------- #
# 5.1 Peer 解析（私有群组 / 话题群 "Peer id invalid"）
# --------------------------------------------------------------------------- #

_PEER_CACHE = {}   # {id(client): 上次预热时间}


def is_peer_error(error):
    """判断是否为 peer 解析失败（会话缓存里没有该群的 access_hash）。"""
    if isinstance(error, PeerIdInvalid):
        return True
    text = str(error).lower()
    return "peer id invalid" in text or "peer_id_invalid" in text


def warm_peer_cache(client, force=False):
    """
    把该会话的对话列表加载进 pyrogram 的 peer 缓存。

    私有群组 / 话题群第一次访问时，会话里没有该群的 access_hash，
    pyrogram 就会抛 `ValueError: Peer id invalid: -100xxxxxxxxxx`。
    """
    if client is None:
        return False
    key = id(client)
    now = time.time()
    if not force and _PEER_CACHE.get(key) and now - _PEER_CACHE[key] < 600:
        return True

    started = now
    count = 0
    try:
        for _ in client.get_dialogs():
            count += 1
    except Exception as exc:  # noqa: BLE001
        log("加载会话列表失败（peer 缓存仍未就绪）：%s" % exc)
        return False

    _PEER_CACHE[key] = time.time()
    log("已加载 %d 个会话到 peer 缓存，用时 %.1f 秒" % (count, time.time() - started))
    return True


def fetch_message(chat_ref, msg_id, client):
    """读取消息；peer 缓存未命中时自动刷新缓存并重试一次。"""
    try:
        return client.get_messages(chat_ref, msg_id)
    except Exception as exc:  # noqa: BLE001
        if not is_peer_error(exc):
            raise
        log("peer 缓存未命中（%s），刷新会话列表后重试…" % exc)
        if not warm_peer_cache(client, force=True):
            raise
        return client.get_messages(chat_ref, msg_id)


def peer_error_text(error):
    """Peer id invalid 的详细中文排查提示。"""
    return (
        "❌ 会话账号还无法访问这个群组（`Peer id invalid`）\n\n"
        "请依次排查：\n"
        "1. 会话账号是否是该群成员：先发送该群的邀请链接 `https://t.me/+xxxx` 让会话加入\n"
        "2. 刚加入的话，等几秒再重新发送一次消息链接（让会话缓存刷新）\n"
        "3. 确认链接是从该群内部复制出来的（形如 `t.me/c/<群组ID>/...`）\n\n"
        "原始错误：`%s`" % error
    )


def iter_message_ids(message, link):
    """把链接里的 ID 范围转成可迭代对象，并按 MAX_RANGE 截断。"""
    total = link.count
    if MAX_RANGE and total > MAX_RANGE:
        reply(message, "注意：一次最多处理 **%d** 条消息，本次只处理前 %d 条。"
                      % (MAX_RANGE, MAX_RANGE))
        total = MAX_RANGE
    return range(link.from_id, link.from_id + total)


# --------------------------------------------------------------------------- #
# 6. 单条 / 相册发送
# --------------------------------------------------------------------------- #

def send_one(message, msg, client, prefix=""):
    """下载并发送单条消息。返回 True 表示成功。"""
    msg_type = get_message_type(msg)

    # 纯文本消息：直接带实体转发
    if msg_type == "Text":
        try:
            bot.send_message(
                message.chat.id, msg.text or "",
                entities=msg.entities,
                reply_to_message_id=message.id,
                disable_web_page_preview=True)
            return True
        except Exception as exc:  # noqa: BLE001
            reply(message, error_text(exc))
            return False

    if msg_type is None:
        reply(message, "跳过：暂不支持该消息类型（投票 / 位置 / 联系方式 / 服务消息等）。")
        return False

    status, task_token, cancel_event = create_cancel_task(
        message, "__正在下载__（点下方按钮可取消）")
    markup = cancel_markup(task_token)
    file_path = None
    upload_path = None
    thumb = None
    try:
        try:
            file_path = client.download_media(
                msg,
                file_name=os.path.join(DOWNLOAD_DIR, ""),
                progress=make_progress(status, "已下载", prefix,
                                       cancel_event=cancel_event, markup=markup),
            )
        except StopTransmission:
            reply(message, "已取消，临时文件已清理。")
            return False
        except Exception as exc:  # noqa: BLE001
            reply(message, error_text(exc))
            return False

        if not file_path:
            reply(message, "❌ 下载失败，已跳过。")
            return False

        # 上传前：改名为「原名_时间戳」；视频/图片重新编码以改变文件哈希
        upload_path = prepare_upload_file(file_path, msg_type)

        try:
            bot.edit_message_text(status.chat.id, status.id, "__正在上传__（点下方按钮可取消）",
                                  reply_markup=markup)
        except Exception:  # noqa: BLE001
            pass

        caption = getattr(msg, "caption", None)
        entities = getattr(msg, "caption_entities", None)
        up = dict(reply_to_message_id=message.id,
                  progress=make_progress(status, "已上传", prefix,
                                         cancel_event=cancel_event, markup=markup))

        if msg_type in ("Document", "Video", "Audio", "Animation"):
            thumb = download_thumb(client, msg)

        if msg_type == "Document":
            bot.send_document(message.chat.id, upload_path, thumb=thumb,
                              caption=caption, caption_entities=entities, **up)
        elif msg_type == "Video":
            video = msg.video
            bot.send_video(message.chat.id, upload_path, thumb=thumb,
                           duration=video.duration, width=video.width,
                           height=video.height, caption=caption,
                           caption_entities=entities, **up)
        elif msg_type == "Animation":
            bot.send_animation(message.chat.id, upload_path, thumb=thumb,
                               caption=caption, caption_entities=entities, **up)
        elif msg_type == "Sticker":
            bot.send_sticker(message.chat.id, upload_path,
                             reply_to_message_id=message.id)
        elif msg_type == "Voice":
            bot.send_voice(message.chat.id, upload_path, caption=caption,
                           caption_entities=entities, **up)
        elif msg_type == "Audio":
            audio = msg.audio
            bot.send_audio(message.chat.id, upload_path, thumb=thumb,
                           caption=caption, caption_entities=entities,
                           duration=getattr(audio, "duration", 0),
                           performer=getattr(audio, "performer", None),
                           title=getattr(audio, "title", None), **up)
        elif msg_type == "Photo":
            bot.send_photo(message.chat.id, upload_path, caption=caption,
                           caption_entities=entities, **up)

        if cancel_event.is_set():
            reply(message, "已取消，临时文件已清理。")
            return False
        return True
    except StopTransmission:
        reply(message, "已取消，临时文件已清理。")
        return False
    except Exception as exc:  # noqa: BLE001
        reply(message, error_text(exc))
        return False
    finally:
        safe_remove(file_path)
        if upload_path and upload_path != file_path:
            safe_remove(upload_path)
        safe_remove(thumb)
        finish_cancel_task(task_token)
        try:
            bot.delete_messages(message.chat.id, [status.id])
        except Exception:  # noqa: BLE001
            pass


def send_album(message, items, client, prefix=""):
    """
    把同一个相册（媒体组）的多条消息作为一组发送。
    返回 True=已处理（成功或已回退为逐条发送），False=不适合按相册处理。
    """
    if not items or len(items) < 2:
        return False
    if not {get_message_type(item) for item in items} <= ALBUM_TYPES:
        return False  # 含不支持的类型，交给调用方逐条发送

    status, task_token, cancel_event = create_cancel_task(
        message, "__正在下载相册__（点下方按钮可取消）")
    markup = cancel_markup(task_token)
    medias = []
    files = []
    upload_files = []
    total = len(items)
    paths = [None] * total
    gate = {"last_edit": 0.0}

    def _download(index, item):
        prefix_text = "[%d/%d] %s" % (index, total, prefix)
        return client.download_media(
            item,
            file_name=os.path.join(DOWNLOAD_DIR, ""),
            progress=make_progress(status, "已下载", prefix_text,
                                   cancel_event=cancel_event, gate=gate,
                                   markup=markup),
        )

    try:
        # 并发下载（ALBUM_WORKERS 控制并发数，1 为串行）
        if ALBUM_WORKERS > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=min(ALBUM_WORKERS, total)) as pool:
                futures = {pool.submit(_download, index, item): index
                           for index, item in enumerate(items, start=1)}
                try:
                    for future in as_completed(futures):
                        index = futures[future]
                        path = future.result()
                        if not path:
                            raise RuntimeError("第 %d 个文件下载失败" % index)
                        paths[index - 1] = path
                        files.append(path)
                except Exception:  # noqa: BLE001
                    cancel_event.set()   # 让其它并发下载尽快停下
                    raise
        else:
            for index, item in enumerate(items, start=1):
                path = _download(index, item)
                if not path:
                    raise RuntimeError("第 %d 个文件下载失败" % index)
                paths[index - 1] = path
                files.append(path)

        for item, path in zip(items, paths):
            # 上传前：改名 + 重新编码（图片/视频改变哈希）
            path = prepare_upload_file(path, get_message_type(item))
            upload_files.append(path)

            caption = getattr(item, "caption", None) or ""
            entities = getattr(item, "caption_entities", None)

            if item.photo:
                medias.append(InputMediaPhoto(
                    media=path, caption=caption, caption_entities=entities))
            elif item.video:
                medias.append(InputMediaVideo(
                    media=path, caption=caption, caption_entities=entities,
                    width=item.video.width, height=item.video.height,
                    duration=item.video.duration))
            elif item.document:
                medias.append(InputMediaDocument(
                    media=path, caption=caption, caption_entities=entities))
            elif item.audio:
                medias.append(InputMediaAudio(
                    media=path, caption=caption, caption_entities=entities,
                    duration=getattr(item.audio, "duration", 0),
                    performer=getattr(item.audio, "performer", None),
                    title=getattr(item.audio, "title", None)))

        if cancel_event.is_set():
            reply(message, "已取消，临时文件已清理。")
            return True

        try:
            bot.edit_message_text(status.chat.id, status.id,
                                  "__正在上传相册（%d 个文件）__" % len(medias),
                                  reply_markup=markup)
        except Exception:  # noqa: BLE001
            pass

        bot.send_media_group(message.chat.id, medias,
                             reply_to_message_id=message.id)
        return True

    except StopTransmission:
        reply(message, "已取消，临时文件已清理。")
        return True
    except Exception as exc:  # noqa: BLE001
        if cancel_event.is_set():
            reply(message, "已取消，临时文件已清理。")
            return True
        reply(message, "❌ 相册整体发送失败（%s），改为逐条发送。" % exc)
        for item in items:
            send_one(message, item, client, prefix)
        return True
    finally:
        for path in files:
            safe_remove(path)
        for path in upload_files:
            if path not in files:
                safe_remove(path)
        finish_cancel_task(task_token)
        try:
            bot.delete_messages(message.chat.id, [status.id])
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 7. 单条消息处理（含相册判定 / 评论区）
# --------------------------------------------------------------------------- #

def relay_target(message):
    """
    用户会话直接转发时的目标会话：
      私聊里就是「用户与机器人的这个聊天」，用户会话需要用机器人的 ID 才能发进去；
      群组里就用原会话。
    """
    chat = getattr(message, "chat", None)
    if getattr(chat, "type", None) == enums.ChatType.PRIVATE:
        return BOT_ID
    return getattr(chat, "id", None)


def relay_message(message, chat_ref, msg, client, single=False, sent_groups=None):
    """
    能直接转发就直接转发、其次服务器端复制，避免「下载 + 重新上传」。

        auto    : 转发 -> 复制（仅机器人端） -> 失败返回 False
        forward : 只转发
        copy    : 只复制（仅机器人端）
        off     : 不尝试，直接下载上传

    返回 True 表示已经送达。
    """
    mode = RELAY_MODE
    if mode == "off":
        return False

    is_bot = client is bot
    target = message.chat.id if is_bot else relay_target(message)
    if not target:
        return False

    ids = [msg.id]
    group_id = getattr(msg, "media_group_id", None)
    if group_id and not single:
        key = ("relay", str(chat_ref), str(group_id))
        if sent_groups is not None and key in sent_groups:
            return True          # 这个相册已经整组转发过了
        try:
            ids = [item.id for item in client.get_media_group(chat_ref, msg.id)]
        except Exception:  # noqa: BLE001
            ids = [msg.id]
        if sent_groups is not None:
            sent_groups.add(key)

    if mode in ("auto", "forward"):
        try:
            client.forward_messages(target, chat_ref,
                                    ids if len(ids) > 1 else msg.id)
            return True
        except Exception as exc:  # noqa: BLE001
            log("直接转发失败（%s），尝试其它方式…" % type(exc).__name__)

    # 复制只在机器人端使用：用户会话复制出来的消息会被当成用户自己发的，
    # 机器人会反复收到并可能触发循环处理。
    if is_bot and mode in ("auto", "copy"):
        try:
            if len(ids) > 1:
                client.copy_media_group(target, chat_ref, ids[0],
                                        reply_to_message_id=message.id)
            else:
                client.copy_message(target, chat_ref, msg.id,
                                    reply_to_message_id=message.id)
            return True
        except Exception as exc:  # noqa: BLE001
            log("服务器端复制失败（%s），改为下载后上传…" % type(exc).__name__)

    return False


def deliver_message(message, msg, chat_ref, client, single=False,
                    sent_groups=None):
    """把已取到的消息发出去：优先转发/复制，不行再下载上传。返回 True 表示已处理。"""
    if relay_message(message, chat_ref, msg, client, single, sent_groups):
        return True

    group_id = getattr(msg, "media_group_id", None)
    if group_id and not single:
        key = (str(chat_ref), str(group_id))
        if sent_groups is not None:
            if key in sent_groups:
                return True
            sent_groups.add(key)
        try:
            items = client.get_media_group(chat_ref, msg.id)
        except Exception:  # noqa: BLE001
            items = None
        if items and len(items) > 1:
            if send_album(message, items, client):
                return True
            for item in items:
                send_one(message, item, client)
            return True

    return send_one(message, msg, client)


def process_comment(message, chat_ref, post_id, comment_id, client):
    """
    评论区链接（`?comment=xxx`）：下载**评论本身**，而不是原帖。

    Telegram 的评论其实是「关联讨论组」里的消息，所以先用原帖定位讨论组，
    再用评论 ID 取出那条评论。
    """
    try:
        discussion = client.get_discussion_message(chat_ref, post_id)
    except Exception as exc:  # noqa: BLE001
        reply(message,
              "❌ 无法定位该帖子的评论区（帖子可能没有开启评论，或会话账号不在讨论组中）。\n"
              "`%s: %s`" % (type(exc).__name__, exc))
        return

    discussion_chat = getattr(getattr(discussion, "chat", None), "id", None)
    if not discussion_chat:
        reply(message, "❌ 该帖子没有可访问的评论区。")
        return

    try:
        msg = fetch_message(discussion_chat, comment_id, client)
    except Exception as exc:  # noqa: BLE001
        reply(message, error_text(exc))
        return

    if msg is None or getattr(msg, "empty", False):
        reply(message, "评论 `%s` 不存在或已被删除。" % comment_id)
        return

    deliver_message(message, msg, discussion_chat, client, single=True)


def process_message_id(message, chat_ref, msg_id, client, single=False,
                       sent_groups=None, comment_id=None):
    """获取并发送指定消息；同一相册只处理一次。"""
    if comment_id is not None:
        process_comment(message, chat_ref, msg_id, comment_id, client)
        return

    try:
        msg = fetch_message(chat_ref, msg_id, client)
    except Exception as exc:  # noqa: BLE001
        reply(message, error_text(exc))
        return

    if msg is None or getattr(msg, "empty", False):
        reply(message, "消息 `%s` 不存在或已被删除，已跳过。" % msg_id)
        return

    deliver_message(message, msg, chat_ref, client, single, sent_groups)


# --------------------------------------------------------------------------- #
# 8. 各类链接的处理
# --------------------------------------------------------------------------- #

def handle_invite(message, link):
    """邀请链接：让用户的会话加入群组/频道。"""
    client = resolve_session(message)
    if client is None:
        return

    token = link.chat
    url = ("https://t.me/" + token) if token.startswith("+") \
        else ("https://t.me/joinchat/" + token)

    try:
        client.join_chat(url)
        log("会话已加入：%s" % url)
        reply(message, "✅ **已加入该群组/频道**\n现在可以发送群内的消息链接了。")
    except UserAlreadyParticipant:
        reply(message, "提示：会话已经在群组/频道中，无需重复加入。")
    except InviteHashExpired:
        reply(message, "❌ 邀请链接已失效。")
    except InviteHashInvalid:
        reply(message, "❌ 邀请链接无效。")
    except Exception as exc:  # noqa: BLE001
        reply(message, error_text(exc))


def handle_private(message, link):
    """私有频道/群组（t.me/c/...）。"""
    client = resolve_session(message)
    if client is None:
        return

    sent_groups = set()
    for msg_id in iter_message_ids(message, link):
        process_message_id(message, link.chat, msg_id, client,
                           link.single, sent_groups)
        time.sleep(DELAY)


def handle_bot(message, link):
    """机器人聊天里的消息（t.me/b/...）。"""
    client = resolve_session(message)
    if client is None:
        return

    sent_groups = set()
    for msg_id in iter_message_ids(message, link):
        process_message_id(message, link.chat, msg_id, client,
                           link.single, sent_groups)
        time.sleep(DELAY)


def handle_comment(message, link):
    """频道评论区链接（?comment=xxx），只处理评论本身。"""
    client = resolve_session(message)
    if client is None:
        return
    process_message_id(message, link.chat, link.from_id, client,
                       single=True, comment_id=link.comment_id)


def handle_public(message, link):
    """
    公开频道/群组：
      1) 机器人有权限时：先直接转发，不行再服务器端复制（都不落地、不重传）
      2) 仍然不行（受限内容 / 机器人不在群内）：回退到用户会话（同样是先转发、再下载上传）
    """
    sent_groups = set()
    client = "unknown"

    for msg_id in iter_message_ids(message, link):
        msg = None

        try:
            msg = bot.get_messages(link.chat, msg_id)
        except UsernameNotOccupied:
            reply(message, "❌ 该用户名不存在。")
            return
        except UsernameInvalid:
            reply(message, "❌ 用户名格式不正确。")
            return
        except Exception:  # noqa: BLE001
            msg = None

        if msg is not None and not getattr(msg, "empty", False):
            if relay_message(message, msg.chat.id, msg, bot,
                             link.single, sent_groups):
                time.sleep(DELAY)
                continue

        # ---- 回退：用用户会话（同样先尝试直接转发） ----
        if client == "unknown":
            client = resolve_session(
                message,
                "机器人无法直接转发/复制这条内容（可能受限，或机器人不在该频道/群组内）。")
        if client is None:
            return

        process_message_id(message, link.chat, msg_id, client,
                           link.single, sent_groups)
        time.sleep(DELAY)


def dispatch(message, link):
    if link.kind == KIND_INVITE:
        handle_invite(message, link)
    elif link.comment_id:
        handle_comment(message, link)
    elif link.kind == KIND_PRIVATE:
        handle_private(message, link)
    elif link.kind == KIND_BOT:
        handle_bot(message, link)
    else:
        handle_public(message, link)


# --------------------------------------------------------------------------- #
# 9. 命令与消息处理器
# --------------------------------------------------------------------------- #

def send_denied(message):
    user = message.from_user
    user_id = user.id if user else "未知"
    reply(message,
          "❌ **这个机器人只供管理员本人使用**\n\n"
          "你的用户 ID：`%s`\n"
          "如果你是部署者，请把该 ID 填到 `config.json` 的 `OWNER_ID` 后重启机器人。"
          % user_id)


@bot.on_message(filters.command("start"))
def cmd_start(client, message):
    if not is_authorized(message):
        send_denied(message)
        return
    text = (
        "**直接发送链接即可**（公开频道不需要登录）：\n"
        "`https://t.me/频道用户名/123`\n"
        "`https://t.me/c/3568041317/1/1331?single`\n"
        "`https://t.me/频道用户名/1001-1010`\n\n"
        "遇到**私有群组 / 受限内容**时，用 /login 绑定你自己的账号（只需一次）即可下载。\n\n"
        "发送 /help 查看全部支持的格式\n"
        "发送 /id 查看你的用户 ID\n"
    )
    try:
        bot.send_message(message.chat.id, text,
                         reply_markup=_keyboard(),
                         reply_to_message_id=message.id,
                         disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        reply(message, error_text(exc))


@bot.on_message(filters.command(["help", "usage"]))
def cmd_help(client, message):
    if not is_authorized(message):
        send_denied(message)
        return
    # 版本号放在 /help 底部，用反引号包住
    reply(message, HELP_TEXT + ("\n版本：`v%s`" % BOT_VERSION))


@bot.on_message(filters.command("id"))
def cmd_id(client, message):
    """/id 任何人都可用，方便部署者查到自己的用户 ID。"""
    lines = ["**信息查询**", ""]
    user = message.from_user
    if user is not None:
        lines.append("你的用户 ID：`%s`" % user.id)
        lines.append("登录状态：%s" % ("已登录" if SESSIONS.get(user.id) else "未登录"))
        if user.username:
            lines.append("用户名：@%s" % user.username)
    lines.append("当前会话 ID：`%s`" % message.chat.id)
    lines.append("")
    if OWNER_ID is None:
        lines.append("未配置 `OWNER_ID`，机器人目前拒绝所有人使用。")
    elif user is not None and is_allowed(user.id):
        lines.append("你就是管理员（`OWNER_ID`），可以正常使用。")
    else:
        lines.append("本机器人只供管理员本人使用。")
    reply(message, "\n".join(lines))


@bot.on_message(filters.command("login"))
def cmd_login(client, message):
    if not is_authorized(message):
        send_denied(message)
        return
    if message.chat.type != enums.ChatType.PRIVATE:
        reply(message, "为保护账号安全，请在与机器人的**私聊**中发送 /login。")
        return

    parts = (message.text or "").split(None, 1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if arg:
        lowered = arg.lower()
        # /login qr：直接进入扫码登录
        if lowered in ("qr", "qrcode", "scan", "扫码"):
            guard_and_login(message.from_user.id, message, "qr")
            return
        # /login phone：直接进入手机号登录
        if lowered in ("phone", "sms", "手机号"):
            guard_and_login(message.from_user.id, message, "phone")
            return
        # /login <session 字符串>：直接使用已有会话
        login_with_session(message, arg)
        return

    guard_and_login(message.from_user.id, message, "choose")


def guard_and_login(user_id, message, mode="choose"):
    """登录前的公共校验，再按 mode 分发：choose / phone / qr。"""
    state = LOGIN_STATE.get(user_id)
    if state and time.time() - state.get("started_at", 0) < LOGIN_TIMEOUT:
        reply(message, "你正在进行登录中，如需重新开始请先发送 /cancel。")
        return

    discard_login(user_id)

    if SESSIONS.get(user_id):
        reply(message, "你已经登录过了。\n如需更换账号，请先发送 /logout 解绑。")
        return

    if mode == "choose":
        reply(message, LOGIN_CHOICE_TEXT, reply_markup=login_choice_markup())
        return

    if mode == "qr":
        start_qr_login(user_id, message)
        return

    LOGIN_STATE[user_id] = {"step": "phone", "started_at": time.time()}
    reply(message, LOGIN_PROMPT_PHONE)


@bot.on_callback_query(filters.regex(r"^login:"))
def cb_login_choice(client, query):
    """处理「手机号登录 / 扫码登录」按钮。"""
    mode = (query.data or "").split(":", 1)[-1]
    user = query.from_user
    user_id = user.id if user else None
    message = query.message

    if user_id is None or not is_allowed(user_id):
        try:
            query.answer("这个机器人只供管理员本人使用")
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        query.answer()
    except Exception:  # noqa: BLE001
        pass

    # 去掉按钮，避免重复点击
    try:
        bot.edit_message_reply_markup(message.chat.id, message.id, reply_markup=None)
    except Exception:  # noqa: BLE001
        pass

    guard_and_login(user_id, message, mode)


SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_\-+/=]{40,}$")


def login_with_session(message, session_string):
    """直接用会话字符串登录（/login <session>），无需手机号与验证码。"""
    user_id = message.from_user.id
    text = session_string.strip().strip("`'\" ")

    if not SESSION_PATTERN.match(text):
        reply(message,
              "会话字符串格式看起来不对。\n"
              "请确认已完整复制（通常是一长串字母数字，长度 100 以上）；"
              "也可以直接发送 /login 用手机号登录。")
        return

    if not is_valid_session_string(text):
        reply(message,
              "❌ 这个会话字符串不完整（被截断或粘贴时少了字符）。\n\n"
              "完整的一条一般是 360 个字符左右；请重新完整复制，"
              "或直接发送 /login 用手机号登录。")
        return

    discard_login(user_id)

    # 已有会话则先停掉，避免堆叠
    old_client = USER_CLIENTS.pop(user_id, None)
    if old_client is not None:
        try:
            old_client.stop()
        except Exception:  # noqa: BLE001
            pass
        _PEER_CACHE.pop(id(old_client), None)

    try:
        client = run_on_loop(_new_client(
            seed="user_%s" % user_id,
            name="user_%s" % user_id,
            session_string=text,
            in_memory=True,
        ))
        me = client.get_me()
    except Exception as exc:  # noqa: BLE001
        log("用户 %s 用 session 字符串登录失败：%s" % (user_id, exc))
        reply(message,
              "❌ 这个会话字符串无效或已失效。\n`%s: %s`\n\n"
              "请重新生成后再试，或直接发送 /login 用手机号登录。"
              % (type(exc).__name__, exc))
        return

    SESSIONS[user_id] = text
    save_sessions(SESSIONS)
    USER_CLIENTS[user_id] = client

    name = " ".join(filter(None, [getattr(me, "first_name", None),
                                  getattr(me, "last_name", None)]))
    if not name:
        name = getattr(me, "username", None) or str(getattr(me, "id", ""))

    log("用户 %s 通过 session 字符串登录成功（账号 %s）"
        % (user_id, getattr(me, "id", "?")))

    # 会话字符串属于敏感信息，登录成功后删掉用户那条消息
    deleted = False
    try:
        bot.delete_messages(message.chat.id, [message.id])
        deleted = True
    except Exception:  # noqa: BLE001
        pass

    out = ("✅ 登录成功：**%s**\n\n"
           "会话已保存（仅绑定你的用户 ID），现在可以下载私有 / 受限内容了。\n"
           "如需解绑请发送 /logout。" % name)
    if deleted:
        bot.send_message(message.chat.id, out, disable_web_page_preview=True)
    else:
        reply(message, out + "\n\n建议手动删除包含会话字符串的那条消息。")


@bot.on_message(filters.command(["logout"]))
def cmd_logout(client, message):
    if not is_authorized(message):
        send_denied(message)
        return
    user_id = message.from_user.id

    discard_login(user_id)
    existed = SESSIONS.pop(user_id, None) is not None
    save_sessions(SESSIONS)

    user_client = USER_CLIENTS.pop(user_id, None)
    if user_client is not None:
        try:
            user_client.stop()
        except Exception:  # noqa: BLE001
            pass
    _PEER_CACHE.pop(id(user_client), None)

    if existed:
        log("用户 %s 已退出登录" % user_id)
        reply(message, "✅ 已退出登录，你的会话已从服务器删除。")
    else:
        reply(message, "你当前没有保存的会话。\n发送 /login 可以绑定账号。")


@bot.on_message(filters.command(["cancel"]))
def cmd_cancel(client, message):
    user_id = message.from_user.id
    if user_id in LOGIN_STATE:
        discard_login(user_id)
        reply(message, "已取消登录。")
    else:
        reply(message, "当前没有进行中的登录。")


def is_forwarded(message):
    """是否是一条转发消息（Telegram 会标记验证码被分享）。"""
    return bool(getattr(message, "forward_from", None)
                or getattr(message, "forward_from_chat", None)
                or getattr(message, "forward_sender_name", None)
                or getattr(message, "forward_origin", None)
                or getattr(message, "forward_date", None))


def handle_login_step(message):
    """
    处理登录过程中的文本消息（手机号 / 验证码 / 两步验证密码）。
    返回 True 表示这条消息已被登录流程消费。
    """
    user = message.from_user
    if user is None:
        return False
    state = LOGIN_STATE.get(user.id)
    if not state:
        return False

    if time.time() - state.get("started_at", 0) > LOGIN_TIMEOUT:
        discard_login(user.id)
        reply(message, "登录已超时，请重新发送 /login。")
        return True

    # 转发来的验证码一律不使用：Telegram 已将该验证码标记为“已分享”，
    # 拿去 sign_in 只会触发 Incomplete login attempt 风控。
    if is_forwarded(message) and state.get("step") in ("phone", "code", "password"):
        log("用户 %s 转发了一条登录相关消息，已提醒（不消耗验证码）" % user.id)
        reply(message, FORWARDED_CODE_TEXT)
        return True

    text = (message.text or "").strip()
    step = state.get("step")

    if step == "qr":
        # 扫码等待中：若用户改发手机号，则自动切换到手机号登录
        digits = re.sub(r"[\s\-()]", "", text)
        if re.fullmatch(r"\+?\d{6,16}", digits):
            return _login_send_code(message, state, text)
        # 其它文本不拦截，仍然可以正常处理链接
        return False

    if step == "phone":
        return _login_send_code(message, state, text)
    if step == "code":
        return _login_sign_in(message, state, text)
    if step == "password":
        return _login_check_password(message, state, text)
    return False


def delivery_hint(sent_type):
    """根据 Telegram 的投递方式给出准确的提示语。"""
    text = str(sent_type).upper()
    if "APP" in text:
        return ("验证码已通过 **Telegram 官方账号** 发到你的 Telegram 里（不是短信）。\n"
                "打开 Telegram → 聊天列表里的「Telegram」→ 看**最新一条**消息。")
    if "SMS" in text:
        return "验证码已通过 **短信** 发送到该号码，请查看手机短信。"
    if "FLASHCALL" in text or "CALL" in text:
        return "Telegram 会通过 **电话** 告知验证码（来电号码里的数字就是验证码）。"
    if "EMAIL" in text:
        return "验证码已发送到该账号绑定的 **邮箱**。"
    return "验证码已发送，请查收。"


def _login_send_code(message, state, text):
    user_id = message.from_user.id
    phone = re.sub(r"[\s\-()]", "", text)

    if not re.fullmatch(r"\+?\d{6,16}", phone):
        reply(message, "手机号格式不正确，请带国家码重新发送（例如 `+8613800138000`），"
                       "发送 /cancel 可取消。")
        return True

    # 若之前是扫码登录（已有连接中的客户端），先清理掉，避免连接泄漏
    stop = state.get("stop")
    if stop is not None:
        stop.set()
    old_client = state.pop("client", None)
    if old_client is not None:
        try:
            old_client.disconnect()
        except Exception:  # noqa: BLE001
            pass
    _drop_qr(state)

    try:
        login_client = run_on_loop(_new_login_client(
            "login_%s" % user_id, seed="user_%s" % user_id))
    except Exception as exc:  # noqa: BLE001
        log("用户 %s 创建登录客户端失败：%s: %s" % (user_id, type(exc).__name__, exc))
        reply(message, error_text(exc))
        discard_login(user_id)
        return True

    try:
        sent = login_client.send_code(phone)
    except Exception as exc:  # noqa: BLE001
        log("用户 %s 发送验证码失败：%s: %s" % (user_id, type(exc).__name__, exc))
        try:
            login_client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        reply(message, error_text(exc))
        discard_login(user_id)
        return True

    new_hash = getattr(sent, "phone_code_hash", None)
    sent_type = str(getattr(sent, "type", ""))
    log("用户 %s 已向 %s 发送验证码（hash=%s，投递=%s）"
        % (user_id, phone, str(new_hash)[:10], sent_type))
    state.update(
        step="code",
        client=login_client,
        phone=phone,
        phone_code_hash=new_hash,
        resent=False,
        wrong_codes=0,
        expired_count=0,
        sent_at=time.time(),
        started_at=time.time(),
    )
    reply(message, "%s\n\n"
                   "请把验证码**一位一位加空格**发给我，例如 %s\n"
                   "（这样 Telegram 不会把它当成复制粘贴的验证码而拦截）\n"
                   "- 同一个验证码可以反复重试，**填错不会作废**\n"
                   "- 若开启了两步验证，下一步会再向你要密码\n"
                   "- 发送 /cancel 可取消" % (delivery_hint(sent_type), format_code_hint()))
    return True


def normalize_code(text):
    """
    把用户发来的验证码归一化成纯数字。

    推荐用户一位一位加空格输入（`1 2 3 4 5`），这样 Telegram 不会把验证码
    判定为“复制粘贴/已分享”（就是 Incomplete login attempt 风控）。
    空格、短横线、中点、括号都会被去掉。
    """
    return re.sub(r"[\s\-_.·()（）]+", "", str(text or ""))


def format_code_hint():
    """演示“逐位加空格”的输入格式。"""
    return "`1 2 3 4 5`（位数按你实际收到的来）"


def _login_sign_in(message, state, text):
    user_id = message.from_user.id
    login_client = state.get("client")

    if login_client is None:
        discard_login(user_id)
        reply(message, "登录状态已丢失，请重新发送 /login。")
        return True

    code = normalize_code(text)
    if not code.isdigit():
        reply(message,
              "验证码应为纯数字，请重新发送。\n\n"
              "建议**一位一位加空格**，例如 %s，这样 Telegram 不会把它当成\n"
              "“复制粘贴的验证码”而拦截。\n发送 /cancel 可取消。" % format_code_hint())
        return True

    log("用户 %s 提交验证码：length=%d，使用 hash=%s（距发码 %.1f 秒）"
        % (user_id, len(code), str(state.get("phone_code_hash"))[:10],
           time.time() - state.get("sent_at", time.time())))
    try:
        signed_in = login_client.sign_in(
            state.get("phone"), state.get("phone_code_hash"), code)
    except SessionPasswordNeeded:
        state.update(step="password", started_at=time.time())
        reply(message, "该账号开启了两步验证，请发送两步验证密码。\n发送 /cancel 可取消。")
        return True
    except FloodWait as exc:
        seconds = getattr(exc, "value", 60) or 60
        log("用户 %s 登录触发频率限制：%s 秒" % (user_id, seconds))
        reply(message, "触发 Telegram 频率限制，请 %s 秒后再试。" % seconds)
        return True
    except PhoneCodeInvalid:
        # 验证码填错：绝对不能重发，否则会把用户手上那个验证码作废，
        # 导致「一直提示验证码失效」的死循环。
        state["wrong_codes"] = state.get("wrong_codes", 0) + 1
        count = state["wrong_codes"]
        log("用户 %s 验证码不正确（第 %d 次）" % (user_id, count))
        if count >= 5:
            discard_login(user_id)
            reply(message,
                  "❌ 验证码连续输错 5 次，本次登录已结束。\n\n"
                  "请确认号码无误后重新发送 /login。")
            return True
        reply(message,
              "❌ 验证码不正确（第 %d 次）。\n\n"
              "**刚才那条验证码仍然有效，没有重新下发**，请核对后再发一次。\n"
              "建议**一位一位加空格**输入，例如 %s，\n"
              "这样 Telegram 不会把它当成复制粘贴的验证码。\n"
              "如果它已经过期，发送 /login 可重新获取。\n"
              "发送 /cancel 可取消。" % (count, format_code_hint()))
        return True
    except PhoneCodeExpired:
        state["expired_count"] = state.get("expired_count", 0) + 1
        age = time.time() - state.get("sent_at", 0)
        log("用户 %s 的验证码已过期（第 %d 次，距发码 %.1f 秒）"
            % (user_id, state["expired_count"], age))
        # 刚发出去几秒就报“过期”，几乎可以肯定是用户用了聊天记录里较早的那条码。
        # 此时绝不能自动重发：再发一次会把用户手上那条也作废，形成死循环。
        if age >= 120 or state["expired_count"] >= 3:
            resend_login_code(message, state)
        else:
            reply(message,
                  "❌ 这条验证码已经失效了。\n\n"
                  "验证码可能有多条，**每次重新发码都会让旧码作废**，"
                  "所以请务必使用**最新一条**（Telegram 官方账号里最下面那条）。\n\n"
                  "另外：如果你之前**转发**或截图分享过 Telegram 的验证码消息，"
                  "Telegram 会把它标为已分享并直接作废，这种情况要等 10~30 分钟再试。\n\n"
                  "如果确认用的就是最新的、也没转发过，发送 /login 可以重新获取。\n"
                  "发送 /cancel 可取消。")
        return True
    except Exception as exc:  # noqa: BLE001
        if looks_like_blocked_login(exc):
            log("用户 %s 登录被风控拦截：%s" % (user_id, exc))
            discard_login(user_id)
            reply(message, LOGIN_BLOCKED_TEXT)
        else:
            log("用户 %s 登录失败：%s: %s" % (user_id, type(exc).__name__, exc))
            reply(message, error_text(exc))
            discard_login(user_id)
        return True

    # sign_in 的返回值需要检查（与 pyrogram 自带 authorize() 一致）：
    #   False                -> 该号码还没有 Telegram 账号，需要注册
    #   TermsOfService 对象  -> 新号码注册前要接受服务条款
    if signed_in is False:
        log("用户 %s 的手机号尚未注册 Telegram" % user_id)
        discard_login(user_id)
        reply(message,
              "❌ 这个手机号还没有 Telegram 账号。\n\n"
              "请先在官方客户端用该号码注册，再回来 /login。")
        return True
    if signed_in is not None and type(signed_in).__name__ == "TermsOfService":
        log("用户 %s 的手机号需要先注册并接受服务条款" % user_id)
        discard_login(user_id)
        reply(message,
              "❌ 这是一个全新的号码，需要先在官方客户端完成注册"
              "（接受服务条款）后才能用机器人登录。")
        return True

    log("用户 %s 验证码登录成功" % user_id)
    finish_login(message, login_client)
    return True


def looks_like_blocked_login(error):
    """判断是否为 Telegram 的「登录被风控拦截」提示。"""
    text = str(error).lower()
    return ("shared" in text or "login attempt" in text
            or "sign in was not allowed" in text)


def retry_login_code(message, state, error):
    """兼容入口：现在只有「验证码过期」才需要重发。"""
    user_id = message.from_user.id
    if isinstance(error, PhoneCodeExpired):
        log("用户 %s 的验证码已过期，重新下发" % user_id)
        resend_login_code(message, state)
        return
    # 其它情况（如填错）不重发，避免作废用户手上的验证码
    log("用户 %s 验证码未被接受（%s），不重发" % (user_id, type(error).__name__))
    reply(message,
          "❌ 验证码未被接受。\n\n"
          "刚才那条验证码仍然有效，请重新发送；\n"
          "如果已经过期，发送 /login 可重新获取。")


def resend_login_code(message, state):
    """真的过期了：申请一个全新的验证码（新的 phone_code_hash）。"""
    user_id = message.from_user.id
    client = state.get("client")
    phone = state.get("phone")

    if client is None or not phone:
        discard_login(user_id)
        reply(message, "登录状态已丢失，请重新发送 /login。")
        return

    try:
        sent = client.send_code(phone)
        _apply_new_code(state, sent)
        state["sent_at"] = time.time()
        log("用户 %s 已重新下发验证码（hash=%s）"
            % (user_id, str(state.get("phone_code_hash"))[:10]))
        reply(message,
              "之前的验证码已过期，已重新发送**一个新的验证码**。\n"
              "请把**最新**收到的那条发给我；发送 /cancel 可取消。")
        return
    except FloodWait as exc:
        seconds = getattr(exc, "value", 60) or 60
        log("用户 %s 重发验证码触发频率限制：%s 秒" % (user_id, seconds))
        reply(message, "触发 Telegram 频率限制，请 %s 秒后再试。" % seconds)
        return
    except Exception as exc:  # noqa: BLE001
        log("重新申请验证码失败：%s: %s" % (type(exc).__name__, exc))

    discard_login(user_id)
    reply(message,
          "❌ 验证码已过期，且自动重新下发也没有成功。\n\n"
          "常见原因：\n"
          "1. 触发了 Telegram 的发送频率限制，等几分钟再试\n"
          "2. 登录被 Telegram 风控拦截\n\n%s" % LOGIN_BLOCKED_HINT)


def _apply_new_code(state, sent):
    """把新下发的验证码信息写回登录状态。"""
    state["phone_code_hash"] = getattr(
        sent, "phone_code_hash", state.get("phone_code_hash"))
    state["step"] = "code"
    state["resent"] = True
    state["wrong_codes"] = 0
    state["started_at"] = time.time()


def _login_check_password(message, state, text):
    user_id = message.from_user.id
    login_client = state.get("client")

    if login_client is None:
        discard_login(user_id)
        reply(message, "登录状态已丢失，请重新发送 /login。")
        return True

    try:
        login_client.check_password(text)
    except PasswordHashInvalid:
        log("用户 %s 两步验证密码不正确" % user_id)
        reply(message, "密码不正确，请重新发送。")
        return True
    except Exception as exc:  # noqa: BLE001
        log("用户 %s 两步验证失败：%s: %s" % (user_id, type(exc).__name__, exc))
        traceback.print_exc()
        reply(message, error_text(exc))
        discard_login(user_id)
        return True

    log("用户 %s 两步验证通过" % user_id)
    finish_login(message, login_client)
    return True


@bot.on_message(filters.text & ~filters.command(
    ["start", "help", "usage", "id", "login", "logout", "cancel"]))
def save(client, message):
    """核心入口：登录状态机 + 解析消息里的链接并处理。"""
    if not is_authorized(message):
        send_denied(message)
        return

    # 登录流程优先消费（转发来的验证码需要在这里单独提醒，不能被下面静默丢掉）
    if handle_login_step(message):
        return

    # 由用户会话直接转发进来的内容不再处理，避免自己转发自己形成循环
    if is_forwarded(message):
        return

    links = parse_links(message.text or "")
    if not links:
        if message.chat.type == enums.ChatType.PRIVATE:
            reply(message,
                  "没有识别到有效的 Telegram 链接。\n"
                  "请发送类似 `https://t.me/频道用户名/123` 的链接，"
                  "或发送 /help 查看用法。")
        return

    user_id = message.from_user.id if message.from_user else "未知"
    log("收到任务：用户 %s，%d 个链接：%s"
        % (user_id, len(links), message.text.strip()))

    for link in links:
        try:
            dispatch(message, link)
        except FloodWait as exc:
            seconds = getattr(exc, "value", 60) or 60
            reply(message, "触发频率限制，等待 %s 秒后继续。" % seconds)
            time.sleep(seconds)
        except Exception as exc:  # noqa: BLE001
            reply(message, error_text(exc))


# --------------------------------------------------------------------------- #
# 10. 启动
# --------------------------------------------------------------------------- #

def prepare_environment():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    # 清理上次运行残留的临时文件
    for name in os.listdir(DOWNLOAD_DIR):
        safe_remove(os.path.join(DOWNLOAD_DIR, name))


def cleanup_downloads(max_age=None):
    """删除 downloads 中超过 max_age 秒的临时文件（兜底：崩溃 / 取消残留）。"""
    max_age = TEMP_MAX_AGE if max_age is None else max_age
    now = time.time()
    removed = 0
    try:
        names = os.listdir(DOWNLOAD_DIR)
    except Exception:  # noqa: BLE001
        return
    for name in names:
        path = os.path.join(DOWNLOAD_DIR, name)
        try:
            if now - os.path.getmtime(path) > max_age:
                safe_remove(path)
                removed += 1
        except Exception:  # noqa: BLE001
            continue
    if removed:
        log("已清理 %d 个过期临时文件（超过 %d 秒）" % (removed, max_age))


def cleanup_loop():
    while True:
        time.sleep(max(60, CLEANUP_INTERVAL))
        cleanup_downloads()


def check_crypto():
    """检查 TgCrypto：没有它时加解密走纯 Python，大文件会慢很多倍。"""
    try:
        import tgcrypto  # noqa: F401
        log("TgCrypto 已加载：AES 硬件加速已启用")
        return True
    except Exception:  # noqa: BLE001
        log("[警告] 未安装 TgCrypto，加解密走纯 Python，下载/上传会慢 3~10 倍！")
        log("        安装命令：pip install TgCrypto")
        return False


STARTUP_HINTS = {
    "UserDeactivated": (
        "机器人所属的 Telegram 账号已被停用或删除（USER_DEACTIVATED）。\n"
        "    1. 用 @BotFather 发送 /mybots，确认这个 bot 是否还在列表里；\n"
        "    2. 不在列表里 = 已被删除，需要 /newbot 重新创建，再把新的 TOKEN 填进来；\n"
        "    3. bot 还在的话，发送 /revoke 重置 Token，用新 Token 重启；\n"
        "    4. 确认创建该 bot 的账号本身没有被封禁。"
    ),
    "UserDeactivatedBan": (
        "机器人所属账号已被 Telegram 封禁（USER_DEACTIVATED_BAN）。\n"
        "    该 bot 无法恢复，需要用另一个账号重新创建。"
    ),
    "AccessTokenInvalid": (
        "TOKEN 无效（格式对但 Telegram 不认）。\n"
        "    请重新从 @BotFather 复制完整 Token（形如 123456789:AAE...）。"
    ),
    "AccessTokenExpired": (
        "TOKEN 已失效。\n"
        "    在 @BotFather 用 /revoke 重新生成后再试。"
    ),
    "AuthKeyUnregistered": (
        "授权信息失效（会话不存在）。\n"
        "    删除目录下的 *.session 文件，或重新配置 TOKEN。"
    ),
    "ApiIdInvalid": (
        "ID / HASH 与 TOKEN 不匹配。\n"
        "    api_id / api_hash 必须来自 my.telegram.org 的同一个应用。"
    ),
    "ApiIdPublishedFlood": (
        "该 api_id 被 Telegram 限制（滥用检测）。\n"
        "    建议换一个应用，或使用你自己的 api_id。"
    ),
    "ConnectionError": (
        "无法连接到 Telegram 服务器（网络 / DNS / 防火墙 / 代理问题）。\n"
        "    1. 确认服务器能出网：curl -I https://api.telegram.org\n"
        "    2. 机房 / 国内环境建议配置 PROXY（如 socks5://user:pass@host:port）\n"
        "    3. 检查防火墙是否放行 443 端口"
    ),
    "TimeoutError": (
        "连接 Telegram 超时。\n"
        "    检查网络或配置 PROXY 后重试。"
    ),
}


def main():
    global BOT_ID

    prepare_environment()

    log("=" * 60)
    log("Save Restricted Bot（中文增强版 by LHL）v%s 启动中…" % BOT_VERSION)
    log("本仓库：%s" % FORK_REPO)
    log("原版仓库：  %s" % ORIGIN_REPO)

    if OWNER_ID is None:
        log("[警告] 未配置 OWNER_ID，机器人将拒绝所有人使用！"
            "请填好 config.json 的 OWNER_ID 后重启")
    else:
        log("仅允许管理员使用：%s" % OWNER_ID)

    log("已保存的用户会话：%d 个" % len(SESSIONS))
    log("单次最多处理消息数：%s，消息间隔：%s 秒"
        % (MAX_RANGE or "不限制", DELAY))
    log("相册并发下载数：%d，临时文件最长保留：%d 秒"
        % (ALBUM_WORKERS, TEMP_MAX_AGE))

    check_crypto()
    threading.Thread(target=cleanup_loop, daemon=True).start()

    log("机器人启动中…")
    try:
        bot.start()
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        log("[错误] 机器人启动失败：%s：%s" % (name, exc))
        hint = STARTUP_HINTS.get(name)
        if hint:
            log("→ " + hint)
        else:
            log("→ 请检查 TOKEN / ID / HASH、网络或 PROXY 配置后重试")
        sys.exit(1)

    try:
        me = bot.get_me()
        BOT_ID = getattr(me, "id", None)
        log("机器人账号：@%s (id=%s)" % (getattr(me, "username", "?"), BOT_ID))
    except Exception as exc:  # noqa: BLE001
        log("获取机器人自身信息失败：%s" % exc)

    # 把命令同步到 Telegram 的指令菜单（用户输入框旁的菜单能直接点选）
    try:
        bot.set_bot_commands(BOT_COMMANDS)
        log("已同步 %d 个命令到机器人指令菜单" % len(BOT_COMMANDS))
    except Exception as exc:  # noqa: BLE001
        log("同步命令菜单失败：%s" % exc)

    log("机器人开始监听…")
    log("=" * 60)
    idle()

    try:
        bot.stop()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
