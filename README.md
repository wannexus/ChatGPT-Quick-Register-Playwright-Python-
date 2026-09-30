# ChatGPT Quick Register (Playwright / Python)

精简的 Python 命令行工具，参考 `codex-oauth-automation-extension-Ultra8.0` 实现。
只做三件事：

1. 用 Playwright 驱动真实 Chromium 跑完 ChatGPT 注册的 1-6 步。
2. 注册成功后在浏览器上下文里 `fetch('/api/auth/session', { credentials: 'include' })`。
3. 把 `{ email, session, ... }` 写入 **MySQL**（密码用 `.env` 中的 Fernet key 加密）；不再写 `output/<邮箱>.json`。

支持：
- **邮箱来源**：
  - `manual` — 手动填邮箱
  - `duck-api` ⭐ — **直接调 DDG Email Protection API**（拿一次 token，永远不用再开 DDG 网页）
  - `duck` — Playwright 打开 DDG 设置页点 Generate（兜底；要先在持久化 profile 里登录过）
  - `icloud` — 用 **iCloud+ 隐藏邮件地址 / Hide My Email** 生成隐私邮箱
  - `mhjc` — 调 **MHJC 临时邮箱 API** 创建 `@mhjc.edu.kg` 邮箱（需要 API Key），
    默认邮箱名按注册姓名随机生成（如 `emma.wilson@mhjc.edu.kg`），不是服务端的 `temp_<hex>`
- **验证码来源**：终端手输 / **QQ 邮箱 IMAP 自动收码** / **MHJC 邮箱 IMAP 自动收码**（不用开网页）

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
- 账号列表表格（数据来自 MySQL）：每行显示有无 token / **本地有效性** / 过期时间 / session 用户邮箱
- 单击「查看」弹出完整 JSON，「复制 accessToken」/「复制 JSON」一键拷贝
- 「检查本地有效性」：只读取本地 session 字段判定（未过期 / 已过期 / 缺 token / 缺 session / 过期未知），**不访问任何远端服务**
- 「刷新 Cookie」：按账号重新登录（`--code-source qq` 或 `mhjc`），成功后只更新该账号在 MySQL 中的 Cookie/session
- 「删除空 session」一键清掉 session 为空的账号记录
- 「复制 access_token」一键导出**所有有效**账号的 token 列表
- **「Codex 登录并推送」**：在账号表勾选账号后，对每个账号依次执行「重新登录 ChatGPT → 获取**新的** Codex OAuth 凭据（含 refresh_token）→ 写入 MySQL → 推送到 SUB2API」。
  逐账号显示 登录 / OAuth / 已保存 / 推送 状态；**登录或 OAuth 失败时不会推送，也不会复用账号里的旧凭据**
- **「检测封禁」**：扫描账号邮箱里的 OpenAI 停用通知，把命中的账号在 MySQL 里标记为封禁（见下节）
- **「SUB2API 设置」**：打开设置时从项目 `.env` 自动读取 URL/凭据并获取分组；分组通过下拉框选择，修改 URL 或 API Key 后会防抖刷新。管理员 API Key、可选 JWT 兜底邮箱/密码、分组及并发数 / 优先级 / 速率倍率 / 隐私模式都保存在 `.env`（`QR_SUB2API_*`），密钥只写入服务端、不回传页面

### 封禁检测

被封禁的 ChatGPT 账号会在登录时直接显示停用页，OpenAI 也会发一封停用通知邮件。两者都会被识别：

- **邮箱扫描（主动轮询）**：`python register.py --mode ban-scan`，或账号页「检测封禁」按钮、`POST /api/accounts/ban-scan`。
  - QQ 收件箱扫一次，覆盖所有 duck / iCloud 转发账号；每个 MHJC 账号用它自己存在 MySQL 里的邮箱密码扫自己的收件箱
  - 只认强特征（`account has been deactivated` / `account_deactivated` / 「没有账户，因为该账户已被删除或停用」/ `账号已被停用` 等），普通验证码邮件和“新设备登录提醒”不会误判
  - 命中后在该账号记录上写 `banned` / `banReason` / `banSubject` / `banSource` / `banDetectedAt`，并把 `reloginRequired` 置回 false
  - **只标记、不删除**，也**不会自动清除**已有标记（避免窗口太短把标记擦掉）；账号的 session / codexAuth 原样保留
  - `--ban-dry-run` 只报告命中、不写库；`--ban-id` 只扫指定账号；`--ban-since-days` 控制回看天数（默认 30）
- **登录时识别（被动）**：刷新 Cookie 或推送时如果页面是停用页，会立刻报出「账号已被 OpenAI 停用」并同时打上封禁标记，而不是像以前那样等到超时
- 已标记封禁的账号**不会再被推送 SUB2API**（避免创建死号），日志与状态表格里会显示原因

### 短信设置与 Codex 自动接码

- 主导航的「短信设置」管理 5sim API Key、服务名、单价上限、候选数量和报价排序；
  同时保留余额、报价、订单轮询、取消、完成及号码池操作。密钥保存到服务端 `.env`，页面只显示配置状态，
  输入框留空沿用已保存密钥。国家等选项同样写入 `.env`，下次打开会恢复。

- 注册后 OAuth、重新登录 OAuth 和账号页「Codex 登录并推送」共用这些配置，只有实际出现手机号验证时才调用 5sim。
- 每个号码最多绑定 **3 个不同 MySQL 账号**。绑定以页面接受手机 OTP 并离开手机号/验证码步骤为准，
  写入账号的 `codexPhoneNumber`；列表仅显示末四位。后续 OAuth 或推送失败也保留绑定。
- 第 1、2 个账号验证成功后保留原 5sim 订单，后续账号直接沿用同一订单与号码接收新短信，
  不会因为推送 SUB2API 成功而提前完结。绑定第 3 个账号后才提交完结；仅接收本次发送后的新验证码。
  原订单过期或已结束时才尝试重新购买号码；明确无法复用时改购新号。
- 如果页面提示无法发送短信并已切换 WhatsApp（如 “We couldn't send a text message…”），
  立即调用 5sim BAN、移除池中号码，重新购买新号继续当前账号；不会继续等待短信。
  主要检查渠道状态和表单错误：请求短信后被切换到 WhatsApp、SMS 被禁用且 WhatsApp 已选中，
  或表单内出现 WhatsApp 错误提示时直接换号，不依赖页面语言。英文/瑞典语文案匹配保留作兜底。
  换号共用本次购买次数上限。BAN 未确认时会记录日志并排除此号码，已有账号绑定保留。
- MySQL 的账号和号码 advisory locks 覆盖号码选择至 OTP 保存，防止并发任务占用第四个名额。
  等待短信时不持有账号行锁；提交绑定时才锁定当前行。需要支持多个命名锁的 MySQL 5.7+。
- 满额的池中号码跳过；新购返回满额号码时取消订单并换号。每次手机号验证最多 **3 次购买请求**，
  包括无库存和被拒绝的尝试；单价上限不等于总预算。无法确认结果的购买/复用请求不会自动重放。
- 单价上限不会阻止沿用已付费的活跃订单。原订单失效后调用号码复用接口属于重新购买，
  该接口不支持请求限价；返回报价超限时尝试取消，再改走限价购买。需严格控制扣费时应同时配置 5sim 服务端价格上限。
- **只在勾选的供应商里买号，并按你排的优先级依次尝试**：
  在「短信设置 → 查询价格」的结果表里勾选「选用」，每勾一个就加入「已选供应商」列表；
  列表**从上到下就是购买优先级**，可用 ↑ ↓ 调整、✕ 移除。点「按优先级买号」会从第 1 个开始买，
  买不到（无库存）就自动切到第 2 个，日志打印每一次降级；自动接码用的是同一份列表和同一套顺序。
  勾选结果随设置保存到 `.env` 的 `QR_FIVESIM_PROVIDERS`（JSON，顺序即优先级）。
- 「选用」只负责挑选与排序，不会立刻扣费；真正花钱的只有「按优先级买号」和自动接码。
  买号前会先拉一次库存快照，明显没号的供应商直接跳过（省一次买号请求），但**顺序不会被打乱**：
  只要还有一家有货，就按优先级从高到低试。购买次数上限会自动放宽到覆盖整个列表长度，
  避免「勾了一堆却每次都只试前 3 个」。
- 列表为空时退回旧行为：用「国家过滤 / 运营商过滤」当唯一供应商
  （`QR_FIVESIM_COUNTRY` / `QR_FIVESIM_OPERATOR`），降级日志同样打印。
- 需要「列表全买不到时也允许换别家」就勾选「列表全买不到时，才允许换到其它供应商」，
  或 `.env` 设 `QR_FIVESIM_ALLOW_OTHER_PROVIDERS=1` / CLI 加 `--5sim-allow-other-providers`。
  自动接码复用号码池时同样受约束：池里不属于已选供应商的号码会被跳过，不会拿来复用。
- CLI 等价写法：`--5sim-providers 'poland/virtual66,greece/virtual34'`（顺序=优先级），
  或重复写 `--5sim-provider usa/virtual21 --5sim-provider poland/virtual66`（追加项排在已保存列表之前）。
- 买号提速：`5sim` API 默认**直连**（`QR_FIVESIM_USE_PROXY=0`，实测直连约 0.5s/次、经浏览器代理约 3s/次）；
  报价快照缓存 60 秒，同一批多个账号不再重复拉全量价格；元数据请求 10 秒超时、买号 25 秒超时，
  失败立刻换下一个候选而不是干等；等待短信的轮询间隔默认 2 秒（`QR_FIVESIM_POLL_INTERVAL`，
  也可在「短信设置」里调）。号码池里上次成功的 `国家+运营商` 会被优先重试。
- 「5sim 请求使用浏览器代理」只影响短信 API 链路，浏览器仍走自己的代理；
  需要让 5sim 也走代理就加 `--5sim-use-proxy` / `QR_FIVESIM_USE_PROXY=1`。
- 买号/复用返回 502/504 时，日志说明请求已发出但未取得号码，需先在 5sim 核对订单再重试，避免重复扣费。
- **验证码接收方式强制用短信**：add-phone 页在号码下方有一个 segmented-control，可选
  「短信 / Text message / Mensaje de texto」和「WhatsApp」，而且**默认值会变**（按页面语言/实验分组）。
  5sim 的号码只会收到短信，所以自动流程在点提交前一定会显式选中短信（按 radio 的
  `value="sms"` 定位，与页面语言无关，另有英文/西语/中文等文案兜底），点完提交还会再复核一次；
  如果页面上只有 WhatsApp、或复核时发现被切成了 WhatsApp，会**立刻报错并取消订单**，
  而不是白等 120 秒短信超时。日志里会打印
  `[codex-oauth] add-phone: 验证码接收方式 = 短信 / Text message` 作为确认。
  万一页面文案明确显示「验证码已发到 WhatsApp」，日志会直接点明，避免误判成 5sim 没发码。
- 失败账号本批跳过，后续账号继续。已有绑定的账号再次要求手机验证时停止并提示核对，不为它重复买号。
- 未配置 Key 时，普通 OAuth 仍可运行；若遇到手机验证会明确失败。SMS OTP 不写入账号或日志。
- 若 OTP 已接受但 MySQL 保存失败，流程停止并提示核对账号和订单；外部验证无法随数据库错误回滚。

配置示例见 `.env.example` 的 `QR_FIVESIM_*`。CLI 可覆盖：

```bash
python register.py --mode codex-push --push-id 12 \
  --5sim-providers 'poland/virtual66,greece/virtual34' --5sim-product openai \
  --5sim-max-price 0.5 --5sim-acquire-priority price
```

验收包含模拟 MySQL 单测、连接关闭即销毁的 MySQL 临时表检查、OAuth 浏览器页面夹具和模拟 5sim 响应；
未改动真实账号记录，未进行真实付费买号、手机号验证或账号推送。

### 刷新 Cookie 的稳定性

旧实现只用 URL 判断登录是否推进，而当前 ChatGPT 登录在提交邮箱后把**密码/验证码表单渲染在同一个** `auth.openai.com/api/accounts/authorize?...` 地址上——该地址不匹配任何一个 URL 规则，于是健康账号也会一直等到 25 秒超时。现在：

- 判定改为 **DOM 优先、URL 兜底**：密码框 / 验证码框可见即视为已推进，地址不变也能继续
- 后续分支（走 OTP 还是密码、是否需要填验证码）同样按 DOM 决定，不再依赖 URL
- 首步预算从 25 秒提高到 30–60 秒（随 `--relogin-timeout` 调整），仍在邮箱页时才会重提交邮箱
- 失败会给出可执行原因：账号被封禁 / 被 Cloudflare 拦截 / OpenAI 限速，而不是笼统的「超时」

### SUB2API 推送鉴权

账号页推送按上游 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的管理端约定鉴权：

- **首选**：管理员 API Key，请求头 `x-api-key`（`QR_SUB2API_ADMIN_API_KEY`），在 SUB2API 后台生成
- **兜底**：未配置 API Key 时，用管理员邮箱+密码调 `POST /api/v1/auth/login` 取 JWT，再走 `Authorization: Bearer`
- 用到的接口：`GET /api/v1/admin/groups/all`（解析分组）与 `POST /api/v1/admin/accounts`（创建账号）
- 后台返回 `INVALID_ADMIN_KEY` 时说明 Key 失效，需要在后台重新生成
- **PAY.153 支付链接**：从 MySQL 账号记录选择 token，创建 Hosted / PayPal / iDEAL / UPI / PIX 等支付链接，轮询任务状态并把结果写回该记录的 `payurlCheck`
- 凭据从环境变量自动预填（`QR_DUCK_TOKEN / QR_QQ_USER / QR_QQ_PASS / QR_PROXY`）
- 表单选项保存在浏览器 localStorage，下次打开自动填回

### 代理配置

需要认证的代理不用再手写 URL，分别填主机/端口/账号/密码即可：

```bash
python register.py --email-source mhjc --code-source mhjc \
  --proxy-host proxy.example.com --proxy-port 8080 \
  --proxy-user alice --proxy-pass 'p@ss:w/rd'
```

- 也可以在 `.env` 里写 `QR_PROXY_HOST` / `QR_PROXY_PORT` / `QR_PROXY_USER` / `QR_PROXY_PASSWORD`
  （可选 `QR_PROXY_SCHEME`，默认 http），或直接在 WebUI 的「高级 → 代理主机/端口/账号/密码」里填。
- 仍然支持完整的 `--proxy http://user:pass@host:port`；同时提供时**分字段优先**。
- **注册主程序默认就使用代理**：只要设置里配了代理，`register.py` 不带任何代理参数也会走它，
  启动横幅会打印实际生效的代理（密码脱敏）。要临时直连用 `--no-proxy`；
  也可以在设置页取消勾选「启用代理」，保存后同样关闭。
  配置了代理但未填地址时，横幅会明确提示「启用但未配置（将直连）」，避免误以为走了代理。
- 账号密码里的 `@` `:` `/` 等符号会自动做百分号编码，可放心填原始密码。
- 代理密码**不会打印到日志**，也不会出现在 WebUI 显示的命令里（显示为 `***`）；
  浏览器与 HTTP 请求默认走同一个代理；5sim 可通过短信设置单独选择直连。带认证的 HTTP(S) 代理会自动经本地桥接注入
  `Proxy-Authorization`（Chromium 不支持带认证的上游代理时会用到）。
- WebUI 里代理密码框留空表示「保持不变」，不会把已保存的密码清掉。
- **「测试代理」**（设置 → 代理设置）会实时告诉你这个代理**实际出口的 IP**，以及它是
  **家宽 / 住宅**还是**机房 / IDC**：

  ```bash
  # 命令行等价用法（需要一个 IP 归属查询，默认 ip-api.com，失败回落 ipwho.is）
  ./.venv/bin/python -c "
  from core import proxy_test
  import json; print(json.dumps(proxy_test.test_proxy('http://user:pass@host:8080'), ensure_ascii=False, indent=2))
  "
  ```

  判定逻辑与结果含义：

  | 结果 | 含义 |
  |---|---|
  | 家宽 / 住宅 | 出口 IP 未被标记为机房，运营商像普通宽带（`hosting=false`） |
  | 机房 / IDC | 出口 IP 属于云厂商/托管机房（`hosting=true`），风控更严 |
  | 移动网络 | 出口是移动/蜂窝网络（`mobile=true`） |
  | 未知 | 查询被限流或该 IP 无足够归属信息 |

  同时会给出：直连 IP 对比（若出口 IP 与直连相同，说明**代理没起作用**或是透明代理）、
  国家/城市、运营商、ASN，以及该 IP 是否被标记为已知代理/VPN。判定基于公开 IP 情报，
  只是参考，不代表任何具体服务一定接受或拒绝。

PAY.153 默认连接 `https://pay.153.ink`，可用 `QR_PAY153_BASE_URL` 或
`.env` 的 `QR_PAY153_BASE_URL` 覆盖。创建任务前必须选择 MySQL 账号、填写代理池并确认：
该账号的 access token 会发送到配置的第三方服务。Token 不会返回给前端，也不会写入任务日志。

### 浏览器指纹（每次注册一套，默认开启）

不需要任何外部指纹浏览器：默认 `--fingerprint-browser stealth` 用内置指纹层，
**每个账号生成一套自洽的真人画像**——UA 与 UA-CH 品牌/平台、`navigator.platform`、
语言与 `Accept-Language`、时区、屏幕分辨率、`hardwareConcurrency`、`deviceMemory`、
WebGL 厂商/渲染器，并对 `getImageData`、`getClientRects` 加确定性噪声；同时把
`navigator.webdriver` 等自动化痕迹抹掉。画像由随机种子派生，写入账号的
`account_data`（`fingerprintSeed` / `fingerprintPersona` / `fingerprintTimezone` /
`fingerprintLanguage`），所以**同一账号重登、Codex 推送会复用同一指纹**，
不同账号互不相同。

- 切换方式：WebUI「设置 → 高级设置 → 浏览器指纹」，或 CLI `--fingerprint-browser stealth|off`，
  或 `.env` 的 `QR_FINGERPRINT_BROWSER`。
- 想让画像落在特定地区/平台：`--fingerprint-region JP`、`--fingerprint-platform windows`
  （留空=每次随机；可用的地区见 `core/fingerprint.py` 的画像表）。
- 连接后会自动复核一次真实指纹（UA/平台/语言/时区/核心数），不一致会在日志里明确报警，
  不会出现「参数写了但没生效」的假指纹。
- 曾经接入过 AntBrowser 的同学：`--fingerprint-browser ant` 仍然保留（可选、默认关闭），
  但常规注册流程完全不依赖它，也不要求 AntBrowser 在运行。

### Cloudflare 挑战页与「窗口被关掉」的自愈

- 清理账号状态时保留当前浏览器中的 `cf_clearance` / `__cf_bm` / `_cfuvid`，账号登录 cookie 仍会删除。
  localStorage/sessionStorage 在浏览器本地清理，不再访问 3 个 `/blank-clear` 页面。
- Codex OAuth 识别挑战后暂停表单操作，等待正常验证完成；等待期间不重开授权页、提交邮箱或购买号码。
  注册和 OAuth 都使用 `--cloudflare-timeout` 的等待预算；无头模式遇到挑战直接报出原因。

首页（chatgpt.com）和 `auth.openai.com` 在这台机器+当前代理上会返回 Cloudflare 托管挑战页，
实测 30~115s 才自动放行。挑战页**没有任何 Sign up / 密码输入框**，而且标题会按浏览器语言本地化
（泰语 `รอสักครู่...`、韩语 `잠시만 기다리십시오…`、英文 `Just a moment...`），所以判定不靠英文文案：
同时看 `cf_chl_opt` / `challenge-platform/h/b/orchestrate` 等 HTML 令牌、`#challenge-running` /
`div.cf-turnstile` 等 DOM 结构，以及多语言标题/「验证成功，等待站点响应」类正文。
**不会**把挂在 Cloudflare 后面的业务页（例如 `auth.openai.com/email-verification`
「Check your inbox」）误判成挑战：那类页只有 `challenge-platform/scripts/jsd/main.js`
埋点脚本，不是挑战；判定也不会再去点它的 submit（2026-09-30 真机误点事故已修）。

- 挑战出现时会打印「检测到 Cloudflare 托管挑战…请勿关闭该窗口」，并在
  `_wait_for_cloudflare_clear` 里等它过去，**不再把挑战页当成「找不到 Sign up 按钮」**。
  等待预算由 `--cloudflare-timeout`（`.env`：`QR_CLOUDFLARE_TIMEOUT`，默认 180s，实测 30~115s 放行）控制；
  手动在窗口里点验证也可以，通过后自动继续。
- **等待期间会自动推进**：每几秒在 Turnstile 里点一次复选框 / 「我不是机器人」类按钮（只点击、
  不逆向求解）；托管挑战多数会自己放行，交互式 Turnstile 需要这一下点击才会继续。
  挑战停留超过约 25s 且点过验证仍未放行时，会刷新一次页面让挑战重新跑（避免 meta refresh 要等 360s）。
- **step3 / relogin / 邮箱提交后的等待全部认挑战**：挑战页 URL 往往已经是 `auth.openai.com/...`
  ，旧逻辑会误当成「已到密码页」然后超时（2026-09-29 真机 debug 里的韩语挑战页就是这么丢的）。
  现在 `step1`–`step4`、`wait_for_login_step`、`wait_for_url_with_recovery` 都会先等挑战过去，
  再认目标 URL / 表单；挑战等待不占用业务步骤自己的超时预算。
- 万一窗口被关掉 / Chrome 崩溃（Playwright 只会抛
  `TargetClosedError: Target page, context or browser has been closed`，看不出原因），现在会：
  1) `_BrowserWatchdog` 在页面/上下文关闭或崩溃的当下就打印带时间戳的告警；
  2) 判定它是「浏览器没了」而不是页面报错（含 cause 链），自动**重开浏览器重试**
     `--browser-retry` 次（`.env`：`QR_BROWSER_RETRY`，默认 1，0=关闭），
     且**复用同一个邮箱**（MHJC 地址只建一次，验证码仍会发到它）；
  3) 只有次数用尽才判定这一轮失败，并打印完整 traceback。
- 未登录首页的 Sign up 按钮**没有 `data-testid`，文案跟随浏览器语言**（指纹画像会在
  20 种语言/地区之间轮换，真机抓到的波兰语是 `Zarejestruj się za darmo`）。现在：
  文案表覆盖了画像用到的语言；若遇到没收录的语言，只要首页已渲染
  （有指向 `/auth/login` 的链接就算渲染完成，href 与语言无关），就**立刻**改用
  `?screen_hint=signup` 的 auth URL，不再白等 25s。
- **频繁被挑战时先看出口 IP**：同一 IP 连续注册多个账号时 Cloudflare 会提高命中率。
  换代理节点（`--proxy` / `QR_PROXY_*`）通常比加大等待更有效；`cf_clearance` 会在本浏览器
  内复用，但不会跨浏览器 profile 持久化。

启动前最好把环境变量设好（凭据不会写到任何配置文件）：
```bash
export QR_DUCK_TOKEN="your-duck-token"
export QR_QQ_USER="xxx@qq.com"
export QR_QQ_PASS="<16位授权码>"
./start_webui.sh
```

所有设置（邮箱、凭证、高级、代理）都保存在项目根目录的 `.env` 里，
WebUI 的「设置」页面会直接读写它；复制 `.env.example` 为 `.env` 即可。
`.env` 已被 `.gitignore` 忽略，不要上传到 GitHub。
不再使用 `config.local.json`；若该文件仍存在，其中的值会在首次运行时**只是补齐**
`.env` 里还没有的键（不会覆盖已有值），之后可以自行删除。

## 前置条件

### MHJC 临时邮箱（`--email-source mhjc --code-source mhjc`）

用 MHJC Mail API 创建一次性邮箱，验证码通过 **IMAP** 读取（`mail.mhjc.edu.kg:993`，SSL）。

1. 找管理员申请 API Key（文档：<https://api.mhjc.edu.kg/docs.html>）。
2. 把 Key 写进被忽略的 `.env`（不要提交、不要贴到公开渠道）：

```bash
QR_MHJC_API_KEY="<your-api-key>"
# 可选：QR_MHJC_API_BASE / QR_MHJC_IMAP_HOST / QR_MHJC_IMAP_PORT / QR_MHJC_TTL
```

3. 直接跑：

```bash
python register.py --email-source mhjc --code-source mhjc --count 5 --cooldown 30 --no-codex-oauth
```

说明：

- **邮箱名默认按注册档案的姓名生成**（`--mhjc-name-style name`，默认），例如
  `emma.wilson@mhjc.edu.kg`、`emmawilson279@mhjc.edu.kg`：地址与第 5 步填写的账号姓名一致，
  看起来就是普通真人邮箱。服务端「随机生成」给的是 `temp_<hex>` 前缀，一眼临时邮箱、更容易被风控，
  所以只在姓名候选全部被占用时才会回落到它（并打印告警）。
  想要旧行为就加 `--mhjc-name-style provider`（或 `.env` 里 `QR_MHJC_NAME_STYLE=provider`）。
- 需要固定地址时用 `--mhjc-username <name>`（同一用户名只能用一次，被占用会自动加后缀，然后才转姓名生成）。
- 邮箱有效期默认 24 小时（`--mhjc-ttl`，服务端上限 86400 秒）。
- 收码默认 3 秒一次、最多 60 次（`--mhjc-interval` / `--mhjc-max-attempts`）。
- 邮箱密码（创建时由服务端返回，用于 IMAP 登录）会随账号一起持久化到 MySQL 的
  `emailPassword` 字段，以便日后重登时继续收码；它**不会打印到日志**，也**不会被账号详情 API 返回**。
- 也可以用 WebUI 的「MHJC API Key」输入框（留空则用服务端 `.env`）；同一页的
  「留空时的命名方式」可选 `随机英文姓名` / `服务商默认`。
- 重登 MHJC 账号时不需要再传邮箱密码，直接从 MySQL 记录里取：

```bash
python register.py --mode relogin --relogin-id 12 --code-source mhjc
```

### DuckDuckGo（推荐 `--email-source duck-api`）

1. 浏览器先访问 https://duckduckgo.com/email/ 注册并开通 Email Protection。
2. 拿到 **auth_token**（推荐两种方式）：
   - **从浏览器扩展配置导出**：装 DuckDuckGo Privacy Essentials，打开扩展后台 / 配置文件，
     找类似下面的 JSON 段：
     ```json
     "duckduckgo": {
       "auth_token": "your-duck-token",
       "base_url": "https://quack.duckduckgo.com"
     }
     ```
   - **从某个第三方工具的配置里**（比如本项目原参考的 `codex-oauth-automation-extension`，自带 token 字段）。
3. 把 token 喂给脚本：
   ```bash
   export QR_DUCK_TOKEN="your-duck-token"
   python3 register.py --email-source duck-api
   ```
   之后所有运行直接走 HTTP `POST https://quack.duckduckgo.com/api/email/addresses`，毫秒级返回新地址。

> 兜底方案：`--email-source duck` 不需要 token，但要先在持久化 profile（默认 `~/.chatgpt_quick_register/profile/`）里手动登录一次 DDG。

### iCloud 隐私邮箱（`--email-source icloud`）

1. Apple ID 需要开通 iCloud+，并启用「隐藏邮件地址 / Hide My Email」。
2. 先用本项目的持久化浏览器登录一次 iCloud：
   ```bash
   python register.py --email-source icloud --code-source manual
   ```
   首次运行会打开 `https://www.icloud.com/mail/`，请在浏览器里完成登录；脚本默认会等待最多 300 秒。
3. 确认 iCloud 隐藏邮件地址的转发目标是你的 QQ 邮箱；验证码仍然用 QQ IMAP 自动收码。
4. 批量注册推荐默认 `--icloud-fetch-mode always-new`。如果要优先复用已有别名，可改为 `reuse-existing`。

```bash
python register.py --email-source icloud \
  --icloud-host auto \
  --icloud-fetch-mode always-new \
  --code-source qq \
  --qq-user xxx@qq.com \
  --qq-pass <16位授权码>
```

### QQ 邮箱 IMAP（用 `--code-source qq` 时）

1. 登录 https://wx.mail.qq.com/ → 设置 → 账户 → POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务。
2. 开启 **IMAP/SMTP 服务**，按提示发短信，拿到 16 位 **授权码**。
3. 这个授权码就是 `--qq-pass` 的值（**不是** 你的 QQ 登录密码）。
4. 验证码邮件需要发到这个 QQ 邮箱（建议把 Duck 地址 / iCloud 隐私邮箱 / Cloudflare Routing 转发到 QQ）。
5. WebUI 可先点「测试 QQ IMAP」验证授权码；使用 QQ 自动收码时，启动任务前也会自动预检，认证失败不会创建新邮箱或启动注册。

## 用法

```bash
# 全手动：自填邮箱 + 终端贴验证码
python register.py --email me@example.com

# Duck API 直连生成邮箱（推荐，毫秒级，不开浏览器）+ 终端贴验证码
export QR_DUCK_TOKEN="your-duck-token"
python register.py --email-source duck-api

# Duck API + QQ IMAP 自动收码（最自动化）
python register.py --email-source duck-api \
  --code-source qq \
  --qq-user xxx@qq.com \
  --qq-pass <16位授权码>

# iCloud 隐私邮箱 + QQ IMAP 自动收码
python register.py --email-source icloud \
  --icloud-fetch-mode always-new \
  --code-source qq \
  --qq-user xxx@qq.com \
  --qq-pass <16位授权码>

# 没有 token？兜底用 Playwright 打开 DDG 网页
python register.py --email-source duck

# 指定输出目录
python register.py --email-source duck --out ~/Desktop/accounts

# headless（实验选项：Duck API 可用，但 OpenAI 注册页常被 Cloudflare/Turnstile 拦截）
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
  ├ icloud.py           iCloud Hide My Email - 走已登录的 iCloud Web 会话生成/保留别名
  ├ qq_imap.py          QQ 邮箱 IMAP 收码（支持 INBOX + Junk）
  └ sub2api.py          SUB2API 传输层：登录 / 解析分组 / 转换 payload / 创建账号
data/
  └ names.py            随机姓名 / 生日 / 强密码生成
webui/
  ├ server.py           FastAPI 后端（批量 / SSE 日志 / 账号 CRUD）
  └ static/index.html   单页前端（无需打包）
output/                 运行期调试/中间产物（账号数据已迁到 MySQL）
requirements.txt
pyproject.toml
```

## 账号存储（MySQL）

账号是唯一持久化来源，存在 MySQL 的 `accounts` 表：`id` 稳定主键、`email` 唯一、
`password_ciphertext`（Fernet 加密）、`account_data`（JSON，**不含密码**）、`relogin_required`、
`validity_status` / `validity_checked_at`。密码只在需要登录时于进程内解密，不会通过 API 返回。

在 `.env`（已被 `.gitignore` 忽略）里配置：

```bash
QR_MYSQL_HOST=127.0.0.1
QR_MYSQL_PORT=3306
QR_MYSQL_USER=root
QR_MYSQL_PASSWORD=
QR_MYSQL_DATABASE=chatgpt_quick_register
# python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
QR_ACCOUNT_ENCRYPTION_KEY=<Fernet key>
```

初始化并自检：

```bash
python -m core.account_store init    # 建库 + 建表
python -m core.account_store check   # 只读连通性/表结构自检
```

缺少配置或 key 无效时会直接失败（fail closed），不会退回本地 JSON。

## 账号数据示意（MySQL 中的 account_data）

```json
{
  "email": "abc1234567@duck.com",
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

- **持久化 profile**：默认走 `chromium.launch_persistent_context`，浏览器状态会尽量保留。要每次干净环境就加 `--no-persistent`。
- **iCloud 隐私邮箱**：参考原扩展的 Hide My Email 链路，通过 iCloud Mail 页面上下文校验会话、解析 `premiummailsettings` 服务节点，再调用 `hme/generate` + `hme/reserve`。iCloud 模式必须使用持久化 profile。
- **session 抓取走浏览器自身**：直接 `page.evaluate("fetch('/api/auth/session', { credentials: 'include' })")`，自动带上当前页面的 cookie，比 `requests` + 手工塞 cookie 稳得多。
- **QQ IMAP 而非浏览器轮询**：原扩展里要打开 QQ 网页轮询 DOM；这里直接 IMAP 连 `imap.qq.com:993`，按 `Date` 头筛掉 run 之前的旧邮件，避免重放老验证码。
- **匹配规则与原项目一致**：发件人 `openai/noreply/verify/auth/duckduckgo/forward`，主题 `verify/verification/code/验证/confirm`。

## 已知限制 / 故障排查

- **DOM 漂移**：OpenAI / DDG 改版会让选择器失效。`core/flow.py` / `core/duck.py` 里都是文本/属性模糊匹配，遇到要改对应函数。
- **Cloudflare / Arkose 风控**：有头模式会等待你手动完成验证；headless 容易被重新挑战，即使有头模式跑通过一次，也不一定会得到可复用的 `cf_clearance`。
- **手机号验证 / Onboarding 弹窗**：本项目不处理。如果第 5/6 步页面停在「需要手机验证」，请自行扩展 `core/flow.py`。
- **资料页（about-you）年龄 vs 出生日期**：OpenAI 有时只问「年龄」，有时问「出生日期」，字段类型还可能是
  `type=date`、带 `mm/dd/yyyy` 占位符的文本框、或「日/月/年」三格下拉。现在会先识别页面到底要哪一种
  （`core/flow.py` 的 `_classify_input` / `choose_birthday_strategy`），再填对应值：年龄填整数，
  日期按占位符提示的格式优先尝试（`yyyy-mm-dd` / `mm/dd/yyyy` / `dd/mm/yyyy` / 年月日）。若某个格式被
  表单校验拒绝，会利用校验反馈换下一种格式重填，而不是盲目重复提交。名字字段填完后若日期字段才出现
  （渐进显示），会自动重新扫描页面，并在提交前确认该字段已填。
- **授权码 ≠ 密码**：QQ IMAP 一定要用 16 位授权码，不是 QQ 登录密码；用错会一直 LOGIN failed。

## 与原项目的关系

整体步骤划分、Duck 生成方式、QQ 邮件过滤规则、session 抓取思路都参考自
[codex-oauth-automation-extension-Ultra8.0](../codex-oauth-automation-extension-Ultra8.0/)。
本项目独立用 Python + Playwright 重写，**不是浏览器扩展**，能写到任意路径，
也不需要 Chrome 加载未签名扩展。
