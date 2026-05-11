# ChatGPT Quick Register (Playwright / Python)

精简的 Python 命令行工具，参考 `codex-oauth-automation-extension-Ultra8.0` 实现。
只做三件事：

1. 用 Playwright 驱动真实 Chromium 跑完 ChatGPT 注册的 1-6 步。
2. 注册成功后在浏览器上下文里 `fetch('/api/auth/session', { credentials: 'include' })`。
3. 把 `{ email, password, session, ... }` 写到 `output/<邮箱>.json`（路径可改）。

支持：
- **邮箱来源**：
  - `manual` — 手动填邮箱
  - `duck-api` ⭐ — **直接调 DDG Email Protection API**（拿一次 token，永远不用再开 DDG 网页）
  - `duck` — Playwright 打开 DDG 设置页点 Generate（兜底；要先在持久化 profile 里登录过）
- **验证码来源**：终端手输 / **QQ 邮箱 IMAP 自动收码**（不用开 QQ 网页）

## 安装

```bash
cd chatgpt-quick-register
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium     # 装一次 Chromium
```

## Web UI（推荐）

```bash
./start_webui.sh                # 默认 http://127.0.0.1:8765/
# 或者：python3 webui/server.py
```

打开浏览器访问 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)，可视化做这些事：

- 配置批量参数（批量数 / 冷却秒数 / 邮箱来源 / 验证码来源 / OTP-vs-密码）后一键开跑
- 实时日志流（SSE，颜色区分 step / session / 成功 / 失败）
- 账号列表表格：每行显示有无 token / 过期时间 / session 用户邮箱
- 单击「查看」弹出完整 JSON，「复制 accessToken」/「复制 JSON」一键拷贝
- 「删除空 session」一键清掉 session 为空的账号文件
- 「复制 access_token」一键导出**所有有效**账号的 token 列表
- **「→ SUB2API」**：把 `output/*.json` 里的有效账号批量转成 SUB2API payload，可**下载 JSON 文件**（手动导入）或**直接 POST 到 SUB2API**（用管理员邮箱密码 + 分组名一键推送）
- 凭据从环境变量自动预填（`QR_DUCK_TOKEN / QR_QQ_USER / QR_QQ_PASS / QR_PROXY`）
- 表单选项保存在浏览器 localStorage，下次打开自动填回

启动前最好把环境变量设好（凭据不会写到任何配置文件）：
```bash
export QR_DUCK_TOKEN="adzkgrg47v..."
export QR_QQ_USER="xxx@qq.com"
export QR_QQ_PASS="<16位授权码>"
./start_webui.sh
```

也可以复制 `config.local.example.json` 为 `config.local.json`，把本机凭据只写在本地配置里。
`config.local.json` 已被 `.gitignore` 忽略，不要上传到 GitHub。

## 前置条件

### DuckDuckGo（推荐 `--email-source duck-api`）

1. 浏览器先访问 https://duckduckgo.com/email/ 注册并开通 Email Protection。
2. 拿到 **auth_token**（推荐两种方式）：
   - **从浏览器扩展配置导出**：装 DuckDuckGo Privacy Essentials，打开扩展后台 / 配置文件，
     找类似下面的 JSON 段：
     ```json
     "duckduckgo": {
       "auth_token": "adzkgrg47v...",
       "base_url": "https://quack.duckduckgo.com"
     }
     ```
   - **从某个第三方工具的配置里**（比如本项目原参考的 `codex-oauth-automation-extension`，自带 token 字段）。
3. 把 token 喂给脚本：
   ```bash
   export QR_DUCK_TOKEN="adzkgrg47v..."
   python3 register.py --email-source duck-api
   ```
   之后所有运行直接走 HTTP `POST https://quack.duckduckgo.com/api/email/addresses`，毫秒级返回新地址。

> 兜底方案：`--email-source duck` 不需要 token，但要先在持久化 profile（默认 `~/.chatgpt_quick_register/profile/`）里手动登录一次 DDG。

### QQ 邮箱 IMAP（用 `--code-source qq` 时）

1. 登录 https://wx.mail.qq.com/ → 设置 → 账户 → POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务。
2. 开启 **IMAP/SMTP 服务**，按提示发短信，拿到 16 位 **授权码**。
3. 这个授权码就是 `--qq-pass` 的值（**不是** 你的 QQ 登录密码）。
4. 验证码邮件需要发到这个 QQ 邮箱（建议把 Duck 地址 / Cloudflare Routing 转发到 QQ）。

## 用法

```bash
# 全手动：自填邮箱 + 终端贴验证码
python register.py --email me@example.com

# Duck API 直连生成邮箱（推荐，毫秒级，不开浏览器）+ 终端贴验证码
export QR_DUCK_TOKEN="adzkgrg47v..."
python register.py --email-source duck-api

# Duck API + QQ IMAP 自动收码（最自动化）
python register.py --email-source duck-api \
  --code-source qq \
  --qq-user xxx@qq.com \
  --qq-pass <16位授权码>

# 没有 token？兜底用 Playwright 打开 DDG 网页
python register.py --email-source duck

# 指定输出目录
python register.py --email-source duck --out ~/Desktop/accounts

# headless（只在确认 Cloudflare 不拦你时再用）
python register.py --email me@example.com --headless

# 也可以从环境变量读 QQ 凭据
export QR_QQ_USER=xxx@qq.com
export QR_QQ_PASS=xxxxxxxxxxxxxxxx
python register.py --email-source duck --code-source qq
```

CLI 完整参数：`python register.py -h`

## 文件结构

```
register.py             CLI 入口
start_webui.sh          Web UI 启动脚本
diagnose_forwarding.py  诊断 DDG → QQ 转发链路
core/
  ├ flow.py             Playwright 6 步主流程（含 OTP 切换 / 重试恢复 / 智能填表）
  ├ session.py          抓 /api/auth/session + 写盘（带 60s 轮询、reload 兜底）— web session，给 Plus 订阅用
  ├ codex_oauth.py      跑一次 codex OAuth 拿完整凭据（access/refresh/id_token + chatgpt_account_id 等）— 给 SUB2API 用
  ├ duck.py             DuckDuckGo Email Protection - 走浏览器（兜底）
  ├ duck_api.py         DuckDuckGo Email Protection - 走 HTTP API（推荐）
  ├ qq_imap.py          QQ 邮箱 IMAP 收码（支持 INBOX + Junk）
  └ sub2api.py          批量转 SUB2API 格式 / 推送账号到 SUB2API 后端，输出 {exported_at, proxies, accounts}
data/
  └ names.py            随机姓名 / 生日 / 强密码生成
webui/
  ├ server.py           FastAPI 后端（批量 / SSE 日志 / 账号 CRUD）
  └ static/index.html   单页前端（无需打包）
output/                 默认输出目录（<邮箱>.json）
requirements.txt
pyproject.toml
```

## 输出 JSON 示意

```json
{
  "email": "abc1234567@duck.com",
  "password": "Xx!2yY3zZ...",
  "savedAt": "2026-05-09T12:00:00.000+00:00",
  "sessionFetchOk": true,
  "sessionStatus": 200,
  "session": { "user": { "...": "..." }, "accessToken": "...", "expires": "..." },
  "sessionRaw": "{...原始文本...}",
  "emailSource": "duck",
  "codeSource": "qq",
  "verificationCode": "123456",
  "name": { "first": "James", "last": "Smith" },
  "birthday": "1992-05-12"
}
```

## 设计要点

- **持久化 profile**：默认走 `chromium.launch_persistent_context`，cookie / Cloudflare 通过验证后会保留，下一次跑就不用重过。要每次干净环境就加 `--no-persistent`。
- **session 抓取走浏览器自身**：直接 `page.evaluate("fetch('/api/auth/session', { credentials: 'include' })")`，自动带上当前页面的 cookie，比 `requests` + 手工塞 cookie 稳得多。
- **QQ IMAP 而非浏览器轮询**：原扩展里要打开 QQ 网页轮询 DOM；这里直接 IMAP 连 `imap.qq.com:993`，按 `Date` 头筛掉 run 之前的旧邮件，避免重放老验证码。
- **匹配规则与原项目一致**：发件人 `openai/noreply/verify/auth/duckduckgo/forward`，主题 `verify/verification/code/验证/confirm`。

## 已知限制 / 故障排查

- **DOM 漂移**：OpenAI / DDG 改版会让选择器失效。`core/flow.py` / `core/duck.py` 里都是文本/属性模糊匹配，遇到要改对应函数。
- **Cloudflare / Arkose 风控**：headed + 持久化 profile 一般能过；headless 容易被拦。被拦时手动在浏览器里点过验证一次，cookie 留下后再继续。
- **手机号验证 / Onboarding 弹窗**：本项目不处理。如果第 5/6 步页面停在「需要手机验证」，请自行扩展 `core/flow.py`。
- **授权码 ≠ 密码**：QQ IMAP 一定要用 16 位授权码，不是 QQ 登录密码；用错会一直 LOGIN failed。

## 与原项目的关系

整体步骤划分、Duck 生成方式、QQ 邮件过滤规则、session 抓取思路都参考自
[codex-oauth-automation-extension-Ultra8.0](../codex-oauth-automation-extension-Ultra8.0/)。
本项目独立用 Python + Playwright 重写，**不是浏览器扩展**，能写到任意路径，
也不需要 Chrome 加载未签名扩展。
