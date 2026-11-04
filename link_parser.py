# -*- coding: utf-8 -*-
"""
Telegram 消息链接解析模块（只依赖标准库，方便单元测试）。

支持的链接形态：

    1. 公开频道/群组      https://t.me/<username>/<msg_id>
    2. 公开批量           https://t.me/<username>/<from>-<to>
    3. 私有频道/群组      https://t.me/c/<chat_id>/<msg_id>
    4. 论坛话题（新群组） https://t.me/c/<chat_id>/<topic_id>/<msg_id>
    5. 带 single 参数     ...?single
    6. 机器人聊天         https://t.me/b/<bot_username>/<msg_id>
    7. 邀请链接           https://t.me/+<hash>  /  https://t.me/joinchat/<hash>
    8. 网页预览           https://t.me/s/<username>/<msg_id>
    9. 短横线两边带空格   https://t.me/<username>/101 - 120
    10. 评论区         ...?comment=<评论ID>  /  ...?thread=<评论ID>
"""

import re

__all__ = [
    "PostLink",
    "INVITE",
    "PRIVATE",
    "BOT",
    "PUBLIC",
    "parse_links",
    "parse_one_link",
    "parse_message_range",
]

# 链接类型
INVITE = "invite"      # 邀请链接，用于加入私有群组/频道
PRIVATE = "private"    # t.me/c/...   需要用户会话
BOT = "bot"            # t.me/b/...   需要用户会话
PUBLIC = "public"      # t.me/<username>/...  优先机器人直接复制

# 链接匹配：兼容 "https://t.me/..."、"t.me/..."、"telegram.me/..."，
# 以及 "t.me/xxxx/101 - 120" 这种短横线两边带空格的写法。
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.me|telegram\.dog)"
    r"/[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+(?:\s*-\s*\d+)?",
    re.IGNORECASE,
)

# 消息 ID 或 ID 范围： "123" / "1001-1010" / "101 - 120"（已去空格）
RANGE_RE = re.compile(r"^(\d+)(?:\s*-\s*(\d+))?$")

# 链接末尾可能粘连的中英文标点，需要裁掉
_TRAILING_CHARS = ".,;:!?)]}>'\"，。、；：！？）】」》’”"


class PostLink:
    """解析后的链接信息。"""

    __slots__ = ("kind", "chat", "from_id", "to_id", "topic_id",
                 "single", "comment_id", "raw")

    def __init__(self, kind, chat, from_id=None, to_id=None,
                 topic_id=None, single=False, comment_id=None, raw=""):
        self.kind = kind          # INVITE / PRIVATE / BOT / PUBLIC
        self.chat = chat          # 用户名(str) 或 chat_id(int) 或邀请 hash(str)
        self.from_id = from_id    # 起始消息 ID
        self.to_id = to_id        # 结束消息 ID
        self.topic_id = topic_id  # 论坛话题 ID（可能为 None）
        self.single = single      # 链接是否带 ?single
        self.comment_id = comment_id  # 评论区链接里的评论消息 ID（可能为 None）
        self.raw = raw            # 原始链接文本

    @property
    def count(self):
        if self.from_id is None or self.to_id is None:
            return 0
        return self.to_id - self.from_id + 1

    def __repr__(self):
        return ("PostLink(kind=%r, chat=%r, ids=%s-%s, topic=%r, single=%r, "
                "comment=%r)"
                % (self.kind, self.chat, self.from_id, self.to_id,
                   self.topic_id, self.single, self.comment_id))


def parse_message_range(token):
    """把 '123' / '1001-1010' / '101 - 120' 解析成 (start, end)，失败返回 None。"""
    if token is None:
        return None
    match = RANGE_RE.match(token.strip())
    if not match:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else start
    if end < start:
        start, end = end, start
    return start, end


def _parse_query(query):
    """
    把 '?single&comment=456' 解析成 {'single': '', 'comment': '456'}。
    同时兼容旧式大小写写法。
    """
    params = {}
    if not query:
        return params
    for part in query.split("&"):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
        else:
            key, value = part, ""
        params[key.strip().lower()] = value.strip()
    return params


def _split_tail(parts):
    """
    从路径中「chat 之后」的部分解析出 (topic_id, from_id, to_id)。

    规则：取最后一个可以解析成数字/范围的片段作为消息 ID，
    如果它前面还有一个纯数字片段，就把它当作论坛话题 ID。

        ['1331']              -> (None, 1331, 1331)
        ['1', '1331']         -> (1, 1331, 1331)      # 话题 1 里的消息 1331
        ['1001-1010']         -> (None, 1001, 1010)
        ['2', '1001-1010']    -> (2, 1001, 1010)
    """
    for index in range(len(parts) - 1, -1, -1):
        rng = parse_message_range(parts[index])
        if not rng:
            continue
        topic_id = None
        if index >= 1 and parts[index - 1].isdigit():
            topic_id = int(parts[index - 1])
        return topic_id, rng[0], rng[1]
    return None


def parse_one_link(raw):
    """解析单条链接，无法识别时返回 None。"""
    if not raw:
        return None

    text = re.sub(r"\s*-\s*", "-", raw.strip()).rstrip(_TRAILING_CHARS)
    if not text:
        return None
    if not text.lower().startswith("http"):
        text = "https://" + text

    # 去掉协议头，只保留 "host/path?query"
    path = text.split("://", 1)[-1]
    path = path.split("/", 1)[1] if "/" in path else ""

    query = ""
    if "?" in path:
        path, query = path.split("?", 1)

    params = _parse_query(query)
    single = "single" in params
    comment_id = None
    for name in ("comment", "thread"):
        value = params.get(name)
        if value and value.isdigit():
            comment_id = int(value)
            break

    parts = [part for part in path.split("/") if part]
    if not parts:
        return None

    head, rest = parts[0], parts[1:]

    # ---- 1. 邀请链接 t.me/+hash / t.me/joinchat/hash ----
    if head.startswith("+") or head.lower() == "joinchat":
        token = rest[0] if head.lower() == "joinchat" and rest else head
        if not token:
            return None
        return PostLink(INVITE, token, raw=raw)
    # ---- 2. 网页预览 t.me/s/<username>/<msg_id> ----
    if head.lower() == "s" and rest:
        head, rest = rest[0], rest[1:]

    # ---- 3. 私有链接 t.me/c/<chat_id>[/<topic_id>]/<msg_id> ----
    if head.lower() == "c":
        if len(rest) < 2:
            return None
        raw_chat = rest[0]
        try:
            chat_id = int(raw_chat) if raw_chat.startswith("-") else int("-100" + raw_chat)
        except ValueError:
            return None
        tail = _split_tail(rest[1:])
        if not tail:
            return None
        topic_id, from_id, to_id = tail
        return PostLink(PRIVATE, chat_id, from_id, to_id, topic_id, single,
                        comment_id, raw)

    # ---- 4. 机器人聊天 t.me/b/<bot_username>/<msg_id> ----
    if head.lower() == "b":
        if len(rest) < 2:
            return None
        tail = _split_tail(rest[1:])
        if not tail:
            return None
        _, from_id, to_id = tail
        return PostLink(BOT, rest[0], from_id, to_id, None, single,
                        comment_id, raw)

    # ---- 5. 公开频道/群组，可能带话题 ----
    tail = _split_tail(rest)
    if not tail:
        return None
    topic_id, from_id, to_id = tail
    return PostLink(PUBLIC, head, from_id, to_id, topic_id, single,
                    comment_id, raw)


def parse_links(text):
    """从一段文本中解析出所有可用的链接，返回 PostLink 列表。"""
    if not text:
        return []
    links = []
    for match in LINK_RE.finditer(text):
        link = parse_one_link(match.group(0))
        if link:
            links.append(link)
    return links
