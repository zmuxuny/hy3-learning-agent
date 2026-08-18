# 邮箱配置与验收

> 状态说明（2026-08-18）：SMTP/IMAP 是正常路径候选，H5-INT/MAIL 与 H6-CONFIG/REDACT 尚未修复。修复前不得把本页当作邮件线程耐久、配置原子性或全链路脱敏保证，也不得用正式邮箱做自动化验收。

Learning Agent 使用 SMTP 发送提醒，使用 IMAP 读取用户对提醒邮件的回复。邮箱密码的配置源是项目根目录 `.env`；设置接口的正常响应不回传密码，但统一错误/事件/Context 脱敏仍待 H6-REDACT-001 验收。

## 先回答：一定要配置 Agent 邮箱 A 吗？

不需要。站内收件箱、页面内新消息提示和手动/自动心跳都不依赖邮箱；只要本地服务持续运行，Agent 就能主动检查并把消息写入应用。

只有希望“应用没开在眼前时也能收到邮件”，才需要一个实际的 SMTP 发件身份。项目无法在没有任何发件凭据的情况下直接进入公共邮件系统。可选方式有：

- 使用现有个人邮箱的应用专用密码作为发件身份；适合个人本地部署，但仍需配置 SMTP。
- 单独准备 Agent 邮箱 A；收发边界最清楚，也是支持回复回原 Session 时最稳妥的方式。
- 使用事务邮件服务提供的 SMTP 凭据；不必新建普通邮箱，但仍需要服务账号和密钥。若还要“回复邮件继续 Session”，该服务必须同时提供可读取回复的邮箱或入站邮件能力，而当前实现直接支持的是 IMAP。

因此，“没有 A 邮箱”可以，“没有任何 SMTP 服务或凭据还能发互联网邮件”不可以。

最容易理解、也最推荐的方式是准备两个地址：

```text
Agent 专用邮箱 A  --SMTP 发送-->  你的日常邮箱 B
Agent 专用邮箱 A  <--IMAP 读取--  你从 B 直接回复
```

采用双地址方案时，你只需要提供 A 的 SMTP/IMAP 授权码；B 只是 `SMTP_TO`，不需要把 B 的密码交给应用。只配置 SMTP 时，Agent 能向 B 发信，但无法理解你的邮件回复；再配置 A 的 IMAP 后，回复才会回到原 Session。不建议让 A 和 B 是同一个地址，否则发送邮件本身可能出现在 IMAP 未读列表里，容易被误判成回复。

## 1. 准备邮箱

在 Agent 专用邮箱 A 的服务商后台开启 SMTP 与 IMAP，并创建“应用专用密码”或“授权码”。不要填写网页登录密码。向邮箱服务商确认 SMTP/IMAP 主机、端口和 TLS 要求。

## 2. 填写 `.env`

```dotenv
SMTP_HOST=服务商的SMTP主机
SMTP_PORT=587
SMTP_USERNAME=Agent专用邮箱A
SMTP_PASSWORD=应用专用密码或授权码
SMTP_FROM=Agent专用邮箱A
SMTP_TO=你的日常邮箱B
SMTP_USE_TLS=true
SMTP_USE_SSL=false

ENABLE_EMAIL_REPLY_POLLING=true
IMAP_HOST=服务商的IMAP主机
IMAP_PORT=993
IMAP_USERNAME=Agent专用邮箱A
IMAP_PASSWORD=应用专用密码或授权码
IMAP_FOLDER=INBOX
```

端口 587 通常使用 `SMTP_USE_TLS=true`、`SMTP_USE_SSL=false`（STARTTLS）；如果服务商要求 465 端口，则改成 `SMTP_PORT=465`、`SMTP_USE_TLS=false`、`SMTP_USE_SSL=true`。两种模式不要同时开启。

修改后重启 `./scripts/start.sh`。打开“收件箱”，邮箱通信卡片应从“等待配置”变成“已配置”。

也可以直接打开侧栏“设置 → 邮件与回复”填写以上字段。旧实现会尝试以 0600 权限更新 `.env`，但 H6-CONFIG-001 已证明控制字符、固定临时文件 symlink 和复杂值往返不安全；H6 修复前应人工编辑并保护 `.env`，不能宣称设置页原子安全保存。页面提供连接测试与删除入口；正常设置响应不返回密码，错误路径脱敏仍待验收。保存后需要重启服务生效。

## 3. 真实连接测试

- “发送测试邮件”：登录 SMTP 并向 `SMTP_TO` 发送一封真实测试邮件。
- “测试回复邮箱”：登录 IMAP，以只读方式打开 `IMAP_FOLDER`。
- 也可以使用 `POST /api/v1/settings/email/test`，请求体为 `{"channel":"smtp","send_message":true}` 或 `{"channel":"imap"}`。

完成连接测试后，可在受控本机用 `notification_send` 的 `email` 渠道验证正常路径。提醒、邮件令牌和回复回 Session 的代码路径已经存在；但活动 Run reply target、多渠道唯一 Intervention、归档计划回执与 IMAP commit-before-Seen 仍由 H5-INT-001–003、H5-MAIL-001/002 阻塞，当前不能保证两种入口在中断/并发下共享同一耐久线程。

## 4. 当前机器状态

`GET /api/v1/settings/email` 会列出缺失字段，但不会返回密码。没有真实邮箱凭据时，SMTP/IMAP 协议行为由自动化替身覆盖，不能冒充外部供应商收发已通过。

## 安全提醒

- `.env` 已被 Git 忽略，不要把凭据写入 README、截图、提交记录或前端代码。
- 建议为 Learning Agent 使用单独邮箱或独立应用密码。
- 本地服务停止时，邮件回复不会被轮询；旧实现重启后会继续扫描未读候选，但 `(RFC822)` 可能在耐久 reply job 前置 Seen（H5-MAIL-002），因此尚不能保证中断恢复不丢回复。
