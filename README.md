<p align="center">
  <img src="https://raw.githubusercontent.com/lhl77/Save-Restricted-Bot/refs/heads/main/img/2.png" width="20%">
</p>

# Save Restricted Bot（中文增强版 by LHL）

把设置了「禁止保存内容」的 Telegram 频道 / 群组 / 论坛话题里的消息转发给你：
公开内容由机器人直接转发或复制，**可选登录**；受限内容则通过你自己的账号会话下载后上传。

- 本项目（中文增强版 by LHL）：<https://github.com/lhl77/Save-Restricted-Bot>
- 原版（bipinkrish）：<https://github.com/bipinkrish/Save-Restricted-Bot>

> 安装、配置、部署与排错请见 [文档](https://blog.lhl.one/artical/1365.html)。

## 快速开始

```bash
git clone https://github.com/lhl77/Save-Restricted-Bot.git
cd Save-Restricted-Bot
python3 -m venv .venv && source .venv/bin/activate     # 需要 Python 3.10 ~ 3.13
pip install -r requirements.txt

# 编辑 config.json，填好 TOKEN / ID / HASH / OWNER_ID
python -u main.py
```

`config.json` 里的字段说明：

```json
{
  "TOKEN": "BotFather 给的机器人 Token",
  "ID": "my.telegram.org 的 api_id（纯数字）",
  "HASH": "my.telegram.org 的 api_hash（32 位十六进制）",
  "OWNER_ID": "你自己的用户 ID，默认只服务这个人",
  "ALLOW_PUBLIC": false,
  "PUBLIC_MAX_FILE_SIZE": 0,
  "MAX_CONCURRENT": 3,
  "MAX_RANGE": 100,
  "DELAY": 3
}
```

| 字段 | 说明 |
| --- | --- |
| `TOKEN` | BotFather 给的机器人 Token |
| `ID` / `HASH` | my.telegram.org 的 `api_id` / `api_hash` |
| `OWNER_ID` | 管理员用户 ID，默认只有这个人能用 |
| `ALLOW_PUBLIC` | **`true` 时对所有人公开可用**；`false`（默认）时只服务 `OWNER_ID` |
| `PUBLIC_MAX_FILE_SIZE` | **除 `OWNER_ID` 以外的用户，单个文件的大小上限（单位 MB）**；`0`（默认）= 不限制 |
| `MAX_CONCURRENT` | 同时进行的下载/上传任务数上限，默认 `3` |
| `MAX_RANGE` | 批量链接单次最多处理多少条 |
| `DELAY` | 每条消息之间的间隔秒数 |

> 键名大小写不敏感（`Allow_Public`、`allow_public` 都能识别）；也可以用环境变量覆盖同名配置。
> **填好真实密钥后不要把 `config.json` 提交到仓库。**

### 公开使用 / 仅自己用

- 默认（`ALLOW_PUBLIC: false`）：只有 `OWNER_ID` 本人能用，其他人一律拒绝；
- 想给别人用：把 `ALLOW_PUBLIC` 改成 `true`，任何人都能发链接给机器人；
- 想限制别人的下载量：调小 `PUBLIC_MAX_FILE_SIZE`（例如 `50` = 单个文件不得超过 50 MB），
  **`OWNER_ID` 本人不受这个限制**；
- 公开模式下会话仍然是**按用户隔离**的：每个人 `/login` 绑定的是自己的账号，
  只能用它下载自己有权访问的内容；没登录的人只能处理公开内容。

## 特性

- **自助登录**：公开内容无需登录；私有 / 受限内容才需要 `/login` 绑定自己的账号，`/logout` 解绑
- **两种登录方式**：`/login` 弹出按钮选择「手机号登录」或「扫码登录」，也可 `/login qr` 直接扫码、`/login phone` 直接手机号；或 `/login <会话字符串>` 用现成的 session string
- **扫码登录**：生成二维码，手机 Telegram「设备 → 连接设备」扫即可，无需输入验证码，二维码过期才原地刷新（不刷屏）
- **任务队列**：全局最多 `MAX_CONCURRENT`（默认 3）个任务同时跑；一条消息发多个链接时，未开始的显示「排队中」并实时刷新（前方任务数 / 已等待时间），点「取消排队」可随时中止
- **管理员免排队**：`OWNER_ID` 的任务直接插到队列最前面
- **`/quere` 管理队列**：仅管理员可用，列出进行中 / 排队中的任务，可单独取消或全部取消
- **上传上限预检**：Telegram 普通账号单文件最大 2000 MiB、Premium 4000 MiB；超过机器人上限时**在下载之前**就判断，
  若你登录的是 Premium 账号，会自动走「收藏夹中转」（转发 / 上传到收藏夹 → 再转发到会话），否则直接提示换账号
- **指令菜单**：启动时自动把 `/start /help /login /logout /id /cancel /quere` 同步到 Telegram 菜单
- **私有群组 / 论坛话题**：支持 `t.me/c/<群组ID>/<话题ID>/<消息ID>`（含新群组大 ID 修复）
- **频道评论区**：`?comment=xxx` 链接下载的是**评论本身**，不是原帖
- **相册整组**：多文件自动整组发送，范围内重复的相册只发一次
- **直传优先**：能直接转发就转发，其次服务器端复制，都不行才下载 + 上传
- **上传前去重**：文件重命名为「原名_时间戳」，视频/图片用 ffmpeg / Pillow 重新编码，改变哈希后再上传；**没装 ffmpeg 时自动跳过重新编码**（只改名），不会卡在转码上，转码过程也可随时取消
- **真实设备指纹**：内置多套真实机型画像，按用户 ID 确定性分配（同一账号永远是同一台设备），并修正 pyrogram 把 `system_lang_code` 写死成 `lang_code` 的指纹缺陷
- **实时进度**：显示已下载大小、百分比与速度，并带「取消」按钮（点击即中断并删临时文件）
- **相册并发下载**：`ALBUM_WORKERS` 控制并发数（自动不超过 `MAX_CONCURRENT`），多文件帖可明显提速
- 全中文提示、可选公开使用（可限制陌生人单文件大小）、链接解析带单元测试

## 支持的链接格式

| 类型 | 示例 |
| --- | --- |
| 公开频道 / 群组 | `https://t.me/username/123` |
| 私有频道 / 群组 | `https://t.me/c/3568041317/1331` |
| 论坛话题（新群组） | `https://t.me/c/3568041317/1/1331?single` |
| 评论区 | `https://t.me/username/123?comment=456` |
| 机器人聊天 | `https://t.me/b/botusername/4321` |
| 批量 | `https://t.me/username/1001-1010` |
| 邀请链接 | `https://t.me/+AbCdEfGhIjK` |

一条消息可发多个链接，会进队列依次处理（最多 3 个同时进行），
未开始的显示「排队中」并可随时取消；链接后紧跟文字或标点也能识别。

## 处理逻辑

1. **权限**：默认只服务 `OWNER_ID` 本人；`ALLOW_PUBLIC: true` 时对所有人开放；`/id` 不受限制。
2. **直传优先**：先试「直接转发」`forward_messages`，成功即完成（不下载、不重传）；转发被禁时在机器人侧改用服务器端复制；都不行才下载 + 上传。
3. **链接类型**：公开链接机器人有权限就自己完成（**不需要登录**）；私有 / 话题 / 机器人 / 评论链接用用户会话（同样先试转发）。
4. **评论区链接**：先用原帖定位关联讨论组，再按 `comment` 消息 ID 取出那条评论并发送。
5. **相册**：同一 `media_group_id` 整组发送；不支持整组时自动逐条发；发送失败也会逐条重试。
6. **批量**：`起始-结束` 超过 `MAX_RANGE` 只处理前 N 条；单条失败只提示并跳过。
7. **队列**：所有链接统一进队列，同时最多 `MAX_CONCURRENT` 个任务；未开始的链接显示「排队中」，管理员任务插队到最前；`/quere` 可取消任意任务。
8. **大小预检**：单文件超过机器人账号上传上限（普通 2000 MiB / Premium 4000 MiB）时在下载前判断——
   用户登录的是 Premium 账号则走「收藏夹中转」（转发 / 上传到收藏夹，再转发到会话），否则直接提示。
   公开用户的 `PUBLIC_MAX_FILE_SIZE` 限制也在此阶段生效。
9. **进度与清理**：下载/上传显示大小、百分比与速度；完成后立即删除本地临时文件。
10. **异常**：`FloodWait` 提示等待；`Peer id invalid` 自动放宽 ID 范围 + 刷新会话缓存后重试。


## 注意事项

- `sessions.json` 存的是**完整账号凭证**，等同于手机登录态，切勿提交或分享；
- 机器人只服务 `OWNER_ID` 本人，请勿关闭该限制后再公开部署；
- 仅供个人备份自己有权限访问的内容，请遵守 Telegram 服务条款与当地法律。

## 致谢

- 原版作者 [bipinkrish](https://github.com/bipinkrish/Save-Restricted-Bot)
- 中文增强版 by LHL：<https://github.com/lhl77/Save-Restricted-Bot>
