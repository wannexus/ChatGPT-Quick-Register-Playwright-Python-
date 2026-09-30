# ChatGPT Quick Register

> Playwright + Python 的 ChatGPT 批量注册与 Codex 凭据工作台：邮箱自动创建 → 注册 6 步 → session / Codex OAuth → MySQL → 可选推送 SUB2API。

**关键词**：Playwright · FastAPI Web UI · MySQL · MHJC / DuckDuckGo / iCloud 临时邮箱 · QQ / MHJC IMAP 自动收码 · 5sim 手机号接码 · 浏览器指纹 · Cloudflare 自愈 · SUB2API

---

## 目录

- [界面一览](#界面一览)
- [它做什么](#它做什么)
- [架构](#架构)
- [快速开始](#快速开始)
- [Web UI](#web-ui)
- [邮箱与验证码](#邮箱与验证码)
- [常用命令](#常用命令)
- [账号存储（MySQL）](#账号存储mysql)
- [进阶能力](#进阶能力)
  - [浏览器指纹](#浏览器指纹)
  - [Cloudflare 挑战页自愈](#cloudflare-挑战页自愈)
  - [短信设置与 Codex 自动接码](#短信设置与-codex-自动接码)
  - [Codex 登录并推送 SUB2API](#codex-登录并推送-sub2api)
  - [封禁检测](#封禁检测)
  - [代理配置](#代理配置)
- [文件结构](#文件结构)
- [设计要点](#设计要点)
- [故障排查](#故障排查)

---

## 界面一览

| 批量注册 | 账号管理 |
| --- | --- |
| ![批量注册](docs/images/webui-home.png) | ![账号管理](docs/images/webui-accounts.png) |

| 短信设置 | 设置 |
| --- | --- |
| ![短信设置](docs/images/webui-sms.png) | ![设置](docs/images/webui-settings.png) |

工具页（账号体检 / 支付链接等）：

![工具](docs/images/webui-tools.png)

---

## 它做什么

1. 用 **Playwright** 驱动真实 Chromium 跑完 ChatGPT 注册 1–6 步（可选每账号一套浏览器指纹）。
2. 注册成功后在浏览器上下文里 `fetch('/api/auth/session', { credentials: 'include' })` 抓 web session。
3. 可选再跑一次 **Codex OAuth**（登录页邮箱 OTP → 凭据），把结果写入 **MySQL**，并推送到 **SUB2API**。

| 能力 | 说明 |
| --- | --- |
| 邮箱来源 | `manual` / **`duck-api`** / `duck` / `icloud` / **`mhjc`**（按真人姓名生成地址） |
| 验证码 | 终端手输 / **QQ IMAP 自动收码** / **MHJC IMAP 自动收码** |
| 认证方式 | OTP 一次性验证码（推荐）或密码 |
| 账号库 | MySQL（密码 Fernet 加密），支持本地有效性检查 / 刷新 Cookie / 检测封禁 |
| Codex | 直接进 Codex 登录页 OTP → 新凭据 → 写库 → 推送 SUB2API |
| 短信 | 5sim 自动接码、供应商优先级、号码池、短信/WhatsApp 渠道强制 |

---

## 架构

![架构](docs/images/architecture.svg)

![注册流程](docs/images/flow.svg)

- `register.py`：CLI / 批量编排入口（也支持 WebUI 以子进程方式拉起）
- `core/flow.py`：注册 1–6 步、Cloudflare 等待、表单恢复
- `core/codex_oauth.py`：Codex OAuth（邮箱 OTP、consent、5sim 手机号）
- `core/num5sim.py`：虚拟号购买 / 轮询短信 / 号码池
- `core/fingerprint.py` + `core/stealth.py`：每账号浏览器指纹
- `webui/`：FastAPI 后端 + 单页前端（SSE 实时日志）

---

## 快速开始

```bash
# 1. 环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# 2. 配置（复制模板后填密钥；.env 已被 gitignore）
cp .env.example .env
# 至少配置 MySQL + Fernet key；按需配置 MHJC / Duck / QQ / 5sim / SUB2API

# 3. 初始化账号库
python -m core.account_store init
python -m core.account_store check

# 4. 启动 Web UI
./restart_webui.sh          # 后台运行，关终端也不断
# 打开 http://127.0.0.1:8765/
```

最小 CLI 示例（MHJC 临时邮箱 + 自动收码）：

```bash
export QR_MHJC_API_KEY="<your-api-key>"
python register.py --email-source mhjc --code-source mhjc --count 2 --cooldown 10 --no-codex-oauth
```

---

## Web UI

```bash
./start_webui.sh            # 前台（调试用）
./restart_webui.sh          # 后台（推荐）
./restart_webui.sh --stop   # 停止
```

默认地址：<http://127.0.0.1:8765/>

| 页签 | 做什么 |
| --- | --- |
| **注册** | 配置批量参数（数量 / 冷却 / 邮箱 / 验证码 / OTP-vs-密码）→ 开跑；SSE 实时日志 |
| **账号** | MySQL 账号表：token / 本地有效性 / 过期 / 重登 / 封禁；查看 JSON、刷新 Cookie、检测封禁、Codex 推送 |
| **工具** | 账号体检、支付链接等辅助工具 |
| **短信设置** | 5sim Key、报价、供应商优先级、号码池、订单轮询/取消 |
| **设置** | MySQL / 邮箱 / QQ / 代理 / 指纹 / SUB2API；一键写入 `.env` |

账号页常用操作：

- **检查本地有效性**：只读本地 session 字段，不访问远端
- **刷新 Cookie**：按账号重新登录，只更新该账号的 Cookie/session
- **复制 access_token**：导出所有有效账号的 token
- **Codex 登录并推送**：直接 Codex 登录页 OTP → 新凭据 → MySQL → SUB2API
- **检测封禁**：扫邮箱里的 OpenAI 停用通知并标记

---

## 邮箱与验证码

### MHJC 临时邮箱（推荐全自动）

```bash
export QR_MHJC_API_KEY="<your-api-key>"
# 可选：QR_MHJC_API_BASE / QR_MHJC_IMAP_HOST / QR_MHJC_IMAP_PORT / QR_MHJC_TTL
python register.py --email-source mhjc --code-source mhjc --count 5 --cooldown 30 --no-codex-oauth
```

- 地址默认按注册姓名生成（`emma.wilson@mhjc.edu.kg`），比服务端 `temp_<hex>` 更像真人邮箱
- 收码走 IMAP（`mail.mhjc.edu.kg:993`），邮箱密码存 MySQL `emailPassword`（不进日志、不进 API）
- 重登直接复用库里的邮箱密码：`python register.py --mode relogin --relogin-id 12 --code-source mhjc`

### DuckDuckGo（`--email-source duck-api`）

1. 在 <https://duckduckgo.com/email/> 开通 Email Protection
2. 取 **auth_token**（扩展配置或第三方工具配置里的 `duckduckgo.auth_token`）
3. `export QR_DUCK_TOKEN="your-duck-token"` 后 `python register.py --email-source duck-api`

> 兜底：`--email-source duck` 不需要 token，但要先在持久化 profile 里登录一次 DDG。

### iCloud 隐私邮箱（`--email-source icloud`）

需要 iCloud+ 与「隐藏邮件地址」。首次运行会打开 iCloud Mail 请你在浏览器登录（默认等 300s），并把别名转发到 QQ。

```bash
python register.py --email-source icloud \
  --icloud-fetch-mode always-new \
  --code-source qq --qq-user xxx@qq.com --qq-pass <16位授权码>
```

### QQ 邮箱 IMAP（`--code-source qq`）

1. QQ 邮箱 → 设置 → 账户 → 开启 IMAP/SMTP，拿 **16 位授权码**（不是登录密码）
2. `--qq-pass` 填授权码；WebUI 可先点「测试 QQ IMAP」
3. 验证码邮件要能到这个 QQ（建议 Duck / iCloud / 转发到 QQ）

---

## 常用命令

```bash
# 全手动
python register.py --email me@example.com

# Duck API + 终端贴码
export QR_DUCK_TOKEN="your-duck-token"
python register.py --email-source duck-api

# Duck + QQ IMAP 全自动
python register.py --email-source duck-api --code-source qq \
  --qq-user xxx@qq.com --qq-pass <16位授权码>

# MHJC 全自动
python register.py --email-source mhjc --code-source mhjc --count 5 --cooldown 30

# 重登已有账号 / Codex 推送 / 封禁扫描
python register.py --mode relogin --relogin-id 12 --code-source mhjc
python register.py --mode codex-push --push-id 7 --code-source mhjc
python register.py --mode ban-scan

# 完整参数
python register.py -h
```

---

## 账号存储（MySQL）

账号唯一持久化来源是 MySQL `accounts` 表：

- `email` 唯一；`password_ciphertext`（Fernet 加密，仅进程内解密）
- `account_data` JSON：session / codexAuth / 姓名生日 / 指纹 / 封禁标记等（**不含密码**）
- `relogin_required`、`validity_status` 等运维字段

`.env` 配置示例：

```bash
QR_MYSQL_HOST=127.0.0.1
QR_MYSQL_PORT=3306
QR_MYSQL_USER=root
QR_MYSQL_PASSWORD=
QR_MYSQL_DATABASE=quick_register
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
QR_ACCOUNT_ENCRYPTION_KEY=<Fernet key>
```

```bash
python -m core.account_store init    # 建库建表
python -m core.account_store check   # 连通性自检
```

配置缺失或 key 无效会直接失败（fail closed），不会退回本地 JSON。

`account_data` 示意：

```json
{
  "email": "emma.wilson@mhjc.edu.kg",
  "session": { "user": { "email": "..." }, "accessToken": "...", "expires": "..." },
  "codexAuth": { "access_token": "...", "refresh_token": "...", "plan_type": "..." },
  "authMode": "otp",
  "name": { "first": "Emma", "last": "Wilson" },
  "birthday": "1992-05-12"
}
```

---

## 进阶能力

### 浏览器指纹

默认 `--fingerprint-browser stealth`：每个账号一套自洽指纹（UA / 语言 / 时区 / 核心数 / Canvas / WebGL…），**同一账号重登与 Codex 推送会复用同一画像**。

```bash
python register.py --fingerprint-browser stealth   # 默认
python register.py --fingerprint-region JP --fingerprint-platform windows
python register.py --fingerprint-browser off       # 关闭
python register.py --fingerprint-browser ant       # 可选 AntBrowser
```

WebUI「设置 → 浏览器指纹」或 `.env`：`QR_FINGERPRINT_BROWSER` / `QR_FINGERPRINT_REGION` / `QR_FINGERPRINT_PLATFORM`。

### Cloudflare 挑战页自愈

首页与 `auth.openai.com` 常见托管挑战（标题随语言本地化：`Just a moment...` / `잠시만 기다리십시오…` / `รอสักครู่...`）。

- **识别**：看挑战令牌（`cf_chl_opt`、`challenge-platform/h/b/orchestrate`、turnstile iframe）+ 多语言标题；**不会**把业务页里的 `jsd` 埋点当成挑战
- **等待**：`--cloudflare-timeout` / `QR_CLOUDFLARE_TIMEOUT`（默认 180s，实测 30~115s 放行）
- **自动推进**：等待期间点 Turnstile 复选框 /「我不是机器人」；卡住会刷新一次
- **step1–4 / relogin / OAuth 都会先等挑战过去**，再认目标 URL/表单
- **浏览器被关掉**：自动重开重试 `--browser-retry` 次（`QR_BROWSER_RETRY`，默认 1），复用同一邮箱
- 频繁被挑战优先换代理出口 IP（`--proxy` / `QR_PROXY_*`）

### 短信设置与 Codex 自动接码

- 管理 5sim API Key、单价上限、报价、号码池、订单轮询/取消
- **只在勾选的供应商里买号**，按列表优先级降级；「选用」不扣费，真正买号的是「按优先级买号」和自动接码
- 每号最多绑 3 个账号；第 3 个绑完才完结订单；池内满额号跳过
- **强制短信渠道**：add-phone 页自动选 SMS（不认 WhatsApp），被切 WhatsApp 会立刻 BAN 换号
- 5sim API 默认**直连**（`QR_FIVESIM_USE_PROXY=0`，更快）；浏览器仍走自己的代理
- 配置项：`QR_FIVESIM_*`、`QR_FIVESIM_PROVIDERS`（JSON，顺序=优先级）

### Codex 登录并推送 SUB2API

账号页勾选 → 「Codex 登录并推送」：

```
直接进入 Codex 登录页邮箱 OTP（不再先登录 chatgpt.com）
  → 取新的 Codex OAuth 凭据（含 refresh_token）
  → 写入 MySQL
  → 推送到 SUB2API
```

- 登录或 OAuth 失败**不会推送**，也**不会复用旧凭据**
- 被封禁账号不推送
- SUB2API URL / 管理员 Key / 分组 / 并发 / 优先级 保存在 `.env` 的 `QR_SUB2API_*`，密钥只在服务端

### 封禁检测

- 邮箱扫描：`python register.py --mode ban-scan`，或账号页「检测封禁」
- 登录时识别停用页并立即标记
- **只标记不删除**，session / codexAuth 保留；已封禁不再推送

### 代理配置

```bash
# .env
QR_PROXY_SCHEME=http
QR_PROXY_HOST=...
QR_PROXY_PORT=...
QR_PROXY_USER=...
QR_PROXY_PASS=...
```

或 CLI `--proxy http://user:pass@host:port`。浏览器与（可选）5sim 共用；WebUI 可「测试代理」。

---

## 文件结构

```
register.py             CLI 入口（register / relogin / codex-push / ban-scan）
restart_webui.sh        后台启动 / 停止 Web UI
start_webui.sh          前台启动 Web UI
core/
  ├ flow.py             注册 6 步 · Cloudflare · 表单恢复
  ├ codex_oauth.py      Codex OAuth · OTP · 手机号
  ├ num5sim.py          5sim 买号 / 接码 / 号码池
  ├ fingerprint.py      指纹画像生成
  ├ stealth.py          Playwright 上下文指纹落地
  ├ ant_browser.py      AntBrowser 接入（可选）
  ├ account_store.py    MySQL 账号库
  ├ session.py          /api/auth/session 抓取
  ├ duck_api.py / duck.py / icloud.py / mhjc.py / qq_imap.py
  ├ sub2api.py          SUB2API 传输层
  └ ban_check.py        封禁文案识别
webui/                  FastAPI + 单页前端
data/names.py           随机姓名 / 生日 / 密码
docs/images/            README 配图
output/                 运行日志与调试快照（不入库）
```

---

## 设计要点

- **持久化 profile**：默认 `chromium.launch_persistent_context`；`--no-persistent` 每次干净环境
- **session 抓取走浏览器自身**：`fetch('/api/auth/session', { credentials: 'include' })`，比手工塞 cookie 稳
- **QQ/MHJC 用 IMAP 而非网页轮询**：按 `Date` 头过滤 run 之前旧邮件，避免重放验证码
- **资料页自适应**：年龄 vs 出生日期、多种日期格式会被识别后填；校验失败会换格式重试
- **失败可诊断**：`output/debug/` 落截图 + HTML（Cloudflare、add-phone、超时等）

---

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 找不到 Sign up / 密码页 | 多半是 Cloudflare；看 `output/debug/*cloudflare*`，延长 `--cloudflare-timeout` 或换 IP |
| 验证码页被误点 submit | 已修：业务页 jsd 埋点不再当成挑战；升级到最新代码 |
| QQ 一直 LOGIN failed | 必须用 16 位**授权码**，不是登录密码 |
| 5sim 等短信超时 | 看是否被切到 WhatsApp；检查供应商是否有货；号码是否被他人占用 |
| 浏览器窗口被关掉 | 会自动重开重试；调大 `--browser-retry` |
| headless 被拦 | OpenAI 注册页对 headless 不友好，建议有头模式 |
| DOM 改版失效 | 改 `core/flow.py` / 对应邮箱模块里的选择器与文案表 |
| MySQL 连不上 | `python -m core.account_store check`；确认 `.env` 与 Fernet key |

---

## 致谢与关系

步骤划分、Duck 生成、QQ 过滤、session 抓取等思路参考
`codex-oauth-automation-extension-Ultra8.0`。本项目是独立的 Python + Playwright 重写，不是浏览器扩展。

---

## License

仅供学习与授权环境下的自动化研究。请遵守目标服务的使用条款与当地法规。
