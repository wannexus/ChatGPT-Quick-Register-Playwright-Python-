# Codex 自动短信接码与 5sim 设置页

## Goal

在账号表的「Codex 登录并推送」流程中自动处理 Codex OAuth 的 add-phone 短信验证；在独立「短信设置」主导航页配置 5sim，同时保留现有报价、余额、订单和号码池工具。一个 5sim 号码最多绑定 3 个**不同 MySQL 账号**；页面接受 OTP 后立刻将号码归属写入该账号记录。号码满额后自动换号。账号本轮失败则跳过，不以同一临时号码给该账号重试。

## Architecture

- `register.py` 继续拥有 Codex 登录编排；`core/codex_oauth.py` 继续拥有 OAuth/add-phone 与 5sim OTP 浏览器适配；`core/num5sim.py` 继续拥有 5sim API 与号码复用候选池；不增加并列 owner。
- MySQL `accounts.account_data` 是账号手机号绑定的唯一持久化事实来源，字段暂定 `codexPhoneNumber`（OTP 被接受后写入）。按电话号码查 distinct account IDs；数量达到 3 时从候选池排除。`output/num5sim_pool.json` 只作为现存号码供给/订单元数据缓存，不拥有账号绑定关系；如需同步次数，只从 MySQL 派生或不再用其次数决定是否可用。
- `core/local_config.py` / `.env` 保存 5sim 凭据和接码选项；`webui/server.py` 仅以 `fiveSimApiKeyPresent` 返回密钥状态，不向 defaults 回传 Key。现有 5sim 面板移入独立「短信设置」tab，其他短信平台可后续加入同一页，本次不实现其他 provider。
- Codex 流程接入当前保存的 5sim 配置；无 Key 且 OAuth 经过 add-phone 时明确失败，不回退假装手动已验证。非 add-phone 的正常 OAuth 行为不受影响。

## Tech Stack

Python 3.9、FastAPI/Pydantic v2、Playwright 异步页面流程、MySQL JSON 账号数据、项目 `.env`、inline JavaScript 主页面、`unittest` 与现有 Playwright fixture harness（若存在）。

## Baseline / Authority Refs

- `docs/aegis/BASELINE-GOVERNANCE.md`：本项目 Aegis 治理约束。
- `docs/aegis/plans/2026-10-04-account-codex-push.md`：Codex 登录推送流程及账号 MySQL owner。
- `docs/aegis/plans/2026-09-28-mysql-account-store.md`：MySQL 唯一账号持久化边界。
- 本轮已批准的对话需求：短信设置独立页；接入 5sim；号码最多给 3 个不同账号；OTP 接受即归属；失败账号跳过；满额自动换号；保留其他短信平台扩展空间。
- `docs/current/AEGIS_MINIMALITY_REFERENCE.md` 不存在于此仓库，按本地可读治理文件继续；本任务重用既有 owners，不新建 owner。

## Compatibility Boundary

- 保持既有 Codex 登录并推送 API、`codex-push` CLI、SUB2API 推送契约及登录/OAuth/保存/推送逐账号状态不变。
- 保持 `QR_FIVESIM_API_KEY` 与既有 `fiveSimApiKey` 配置契约；新增国家、运营商、产品、最高价、候选数等 `.env` keys 采用一致的 `QR_FIVESIM_*` 命名，默认兼容既有 `any/openai/8` 行为。
- 保留现有 5sim 独立接码工具与号码池数据；不删旧字段/数据，不做破坏性迁移。号码绑定字段只追加至 MySQL JSON，不改 MySQL 表结构。
- `GET /api/defaults` 不暴露密钥明文。账号列表只返回足以显示手机号/绑定状态的信息；不增加秘密内容。

## Change Necessity

仅搬 UI 或文档不能让 Codex add-phone 自动取得号码，也无法阻止 5sim 号码被分配给超过 3 个不同账号。需要最小代码变更贯通既有登录接码、号码选择、MySQL 账号 JSON 读写和设置 UI；号码归属在 OTP 通过后的同一账号持久化路径内写入，避免另立绑定数据库或第二份状态源。

## TDD Route

- Mode: auto
- Decision: strict
- Authority: user-approved新功能；MySQL durable account field、producer/consumer contract、外部付费号码购买、Codex 登录流程安全边界。
- Test posture: RED/GREEN/REFACTOR。先为绑定去重、满额排除、OTP 后保存、后续失败保留映射、未获 OTP 不写映射、运行时 5sim 配置、secret 不出 defaults、短信页导航迁移编写失败测试/fixture。
- Reason:跨核心流程、持久化和第三方付费 API；错误可能造成重复绑定、号码额外支出或账号手机号错配。
- Verification:相关单测 + 全量 unittest、`py_compile`、inline JS `node --check`、Playwright mocked UI 与登录 fixture；不得进行 live Codex 登录、5sim 买号或 SUB2API push。

## Ripple Signal Triage

- Canonical owner:账号记录在 `core/account_store.py` / MySQL `account_data`；Codex 编排在 `register.py`；OAuth/add-phone 事件在 `core/codex_oauth.py`；号码API/池在 `core/num5sim.py`；配置在 `core/local_config.py`，HTTP API 在 `webui/server.py`，页面在 `webui/static/index.html`。
- Consumer ripple:账号详情/表格展示（不得泄漏额外秘密）；保存及重登会 round-trip 账号数据；账号选择 Codex Push 必须读取 bound phone map；设置 POST defaults 应能写参数而保留空白 Key 既存语义；CLI 与 Web UI 共用同一有效配置。
- Risk:批量并发时两个 worker 同时选择最后一个名额，可能第4账号获号。实现须以 MySQL 绑定表/锁或单一 Codex push 执行互斥策略确保 check+claim 原子性。不能仅依赖本地 JSON pool 的非原子读写。设计决定的最小路径是 codex-push worker 在真正启动期间跨进程互斥或账号分配事务；优先复用 `webui/server.py`/BatchManager 已有任务互斥，但 CLI 可并发执行，故仍要在持久化 owner 内提供原子 claim。
- Secret / paid side effects:新购必须仅在 add-phone 实际被触发时发生；记录订单/异常不能包含 API Key、OTP 或完整手机号日志。日志以遮蔽号码、订单 ID 和手机号末四位为限。

## Plan Pressure Test

- Owner / contract:复用已有 owner，加入本地数据 JSON 新字段；但原子跨账号 count+claim 将超出纯 JSON row update。计划应在 `AccountStore` 增加最小原子绑定 owner/接口；使用现有 MySQL `accounts` 表锁定号码绑定分配，不新增表，且该行为属 MySQL canonical owner。
- Higher-level simplification:让 MySQL 绑定映射自己决定额度；号码池 JSON 只提供 5sim 候选，不保留冲突的成功使用次数上限，避免两套配额准则。
- Verification:构造并发测试，确保最多3个不同 account IDs；mock 号码池选择和请求，证明满额号码不会被 reuse。
- Task executability:先实现并测 AccountStore 原子 reserve/绑定能力，再接入 OAuth callback 和 UI 配置，最后全链验证。

## Tasks

1. **MySQL 号码绑定 owner 与测试** (`core/account_store.py`, `tests/test_account_store.py`、新增聚焦测试如 `tests/test_fivesim_account_binding.py`)：定义号码字段规范化/唯一账号数/容量 3；实现原子绑定或 reservation，保证不同账号并发最多占 3 位，同账号重复不加名额；同号码同账号重复操作幂等。测试 distinct-ID、边界 2→3、满额拒绝、重复账号、并发、异常回滚。账号数据通过既有 `save_account/get_account/list_accounts` 同一 canonical source 保存；不新增 schema 表。若仅 account_data JSON 行锁无法对“尚未绑定账号”的手机号可靠串行化，必须用 account-store 内事务锁策略对所有号码 claim 序列化，证明竞态测试；不能弱化为单进程锁。
2. **5sim号码挑选与 OTP 绑定提交** (`core/num5sim.py`, `core/codex_oauth.py`, `register.py`，测试新增/既有 OAuth 测试)：由可复用号码池产出候选后，用 MySQL authoritative available/count 过滤；OTP 页面继续离开 phone verification 只视为接受，调用绑定提交并只在成功时落 MySQL；未成功验证、取消、超时不得写账号绑定；遇到号码已达3账号或并发claim失败，排除当前号码并自动换新号继续（有界重试，耗尽时可读失败）。同账号历史绑定手机的本批重试策略遵从用户“失败账号跳过”：批次里每账号仅处理一次，不主动复用已绑定号码给同账号。移除/停用本地 pool 成功使用数作为主配额，保留 pool供给元数据，不并行双重计数。向 verifier 传入 account ID 和 account-store writer callback；不把账号密码或手机号用于临时外部日志。
3. **短信配置与页面迁移** (`core/local_config.py`, `webui/server.py`, `register.py`, `webui/static/index.html`, `tests/test_local_config_env.py`, `tests/test_webui_accounts.py`)：将现有 5sim 面板整体移动至独立主标签「短信设置」，保持 DOM IDs 与现有工具动作行为；配置 Key 使用密钥状态布尔，默认响应不得明文回 Key，密码框空白沿用已存 Key；持久化国家、operator、product、max price、候选数量及与 Codex 选择相关的必要项到 `.env`；删除旧设置中重复的 5sim Key 输入，仅保留唯一 owner，注册/Codex OAuth 接码统一读取配置。为后续短信 provider 留扩展空间，不添加目前无实现的抽象接口。
4. **账号表显示及状态事实** (`webui/server.py`, `webui/static/index.html`, tests)：在账号行增加手机号列或绑定标记，号码安全遮蔽（仅末四位）；账号详情可见完整绑定手机号仅在用户需要验证（需按最少暴露设计，优先仍遮蔽）；Codex push 的既有进度结构不必加号码明文。测试账号 JSON round-trip、摘要只显示脱敏手机号、保留字段不被 `/api/defaults` 回传。
5. **联调与文档验证** (`README.md`, 本计划结果)：文档说明短信配置、OTP 绑定定义、每号三账号、失败跳过、号码切换、付费限制与安全；完整回归、语法检查和 mocked UI / OAuth 验收；不执行真实外部交易，不重启正在运行的 UI 服务除非用户之后明确要求启动/刷新。

## Stop / Risk boundaries

- 默认 maximum SMS spends per account:只有 add-phone 确实触发才买号；每账号一个 Codex-push attempt；添加可配置 max-price 默认不限值延续既有参数；重购有明确 bounded retry，金额/次数尚未由用户指定时采用每次 add-phone 最多 3 个购买订单（含无效/拒绝换号）并报告额度触顶，而不是无限花费。此安全上限须在实现前作为默认策略；配置的 price ceiling 不等于总花费上限。
- MySQL claims are permanent and OTP acceptance consumes one of a number's 3 distinct-account slots even if OAuth/push later fails, as user approved.
- OTP/SMS data must never be logged; show at most masked number.
- No live SMS purchases, real Codex login, account mutation, or SUB2API create during implementation verification.

## Retirement / Compatibility

- Retire the 5sim panel placement under “工具” and duplicated 5sim Key input under general credentials; keep one canonical settings surface under “短信设置”. Retain 5sim independent profile/prices/order operations.
- Retain `output/num5sim_pool.json` as inventory/reuse metadata only; retire its `successful_uses` field as an authority for Codex's 3-distinct-MySQL-account cap. It can be ignored/backward-read; no destructive cleanup or migration.
- Preserve any other existing SMS/email providers untouched. No generic provider framework in this change.

## Verification

- Focused account-store tests include serialized/concurrent reservation with 4 unique account IDs, asserting exactly 3 accepted.
- OAuth mocked tests cover phone prompt -> OTP accepted -> callback persisted exactly once; invalid/failed OTP leaves no mapping; later OAuth or push error keeps saved mapping; full 3 slots triggers a new purchase; repeated same ID cannot consume extra slot; finite buy attempts.
- Config/API tests confirm fields save/read from `.env`, password blank retention, secrets not in defaults/HTML/API logs.
- Playwright mocked navigation confirms “短信设置” independent tab owns existing 5sim widgets and tools page no longer does; settings interactions and mobile layout.
- Run focused suites, `./.venv/bin/python -m unittest discover -s tests`, `py_compile` modified Python modules and `node --check` extracted inline script.
- Prohibited verification: real 5sim order/purchase, real phone OTP submission, real Codex login, real SUB2API account creation.

## Approval / Implementation Route

User approved: “按照方案执行” on 2026-10-05, following accepted product boundaries in conversation. No further design approval needed unless tests prove the account-store atomicity contract cannot be met without a schema/new surface; if so stop and return to design rather than create table/adapter unilaterally.

- TDD: strict as recorded above.
- Execution route: inline; SQL owner, OAuth consumer and UI depend sequentially on one binding contract and shared dirty tree makes parallel writes risky.
- User confirmation required: no for mock/test implementation; yes before any real paid 5sim purchase, live OTP, or external account mutation.

## Results

Implementation and local acceptance completed:

- MySQL `reserve_codex_phone` holds account and phone advisory locks before external verification and yields the sole OTP commit callback. It commits the initial read transaction before waiting for SMS; the account row is locked only during the accepted OTP write. `bind_codex_phone` reuses this contract for idempotent verified writes. Four concurrent fixture workers accept exactly three distinct accounts.
- Normal `save_account` strips caller-provided phone bindings and atomically preserves the existing canonical binding in its upsert. The former test fake that silently preserved data without matching the SQL has been corrected. Fresh session saves, stale snapshots, later failures and cancellation cannot erase a committed binding.
- OAuth skips full inventory entries before reuse, checks newly purchased numbers before submission, cancels full new orders and switches numbers within three purchase requests. Ambiguous purchases/reuse calls are not replayed. Configured price ceilings use the capped purchase endpoint. Legacy pool use counts remain metadata and cannot expire inventory or decide Codex quota.
- Registration saves its account before OAuth; registration, re-login and Codex push all pass the specific account ID and store. OTP acceptance requires leaving both phone and code steps; rejected OTP leaves no binding. A cancellation during the commit waits for the durable write before closing the reservation.
- The separate SMS page owns the single key input and all existing tools. Saved keys work with blank browser inputs; country/operator choices round-trip; manual reuse targets the clicked number. Account list and detail show only the masked bound phone. README and `.env.example` document the workflow and `QR_FIVESIM_*` settings.
- Evidence: 332 unittests pass without the earlier unawaited-test warning; Python and inline JavaScript syntax checks pass; 18/18 Playwright UI checks pass with 27 mocked API requests and desktop/mobile screenshots. `tests/phone_oauth_smoke.py` exercises real DOM transitions with all requests intercepted, proving accepted/rejected OTP behavior and masked logs.
- `tests/mysql_phone_smoke.py` verifies actual upsert SQL, cap rejection, idempotence, preserved binding, abandoned reservation and cross-session lock exclusion in a connection-local temporary table. The configured server also retained two simultaneous named locks. Closing the connection destroys the temporary table; no production account or persistent schema was changed. Concurrent account workers remain covered by the SQL fixture model, rather than a live multi-worker account trial.
- No real 5sim purchase, external OTP, Codex login or SUB2API create was performed. The existing 8765 service was not restarted; a separate temporary 8799 server was used for the intercepted UI checks.

## Self-review

- User-selected behavior and three-distinct-account rule are explicit; OTP acceptance is the binding event; failure skip and auto-new-number are retained.
- MySQL is the only binding authority; pool's legacy count cannot create a second limit owner.
- Need to verify safe transactional implementation without schema expansion before coding; unresolved design issue is an implementation stop condition, not permission to improvise.
- User-visible phone values are masked and secrets remain service-side.
- UI migration retains all prior 5sim tools, removes duplicate key input, and leaves other providers untouched.
- Financial side effects are excluded from tests; bounded purchase retry and existing max price constrain risk.
- No unauthorized edits to unrelated dirty files; no service restart implied.

## Initial Source Evidence

- `register.py`: `_build_5sim_phone_verifier`, `_run_one_codex_push`, `--5sim-*` arguments.
- `core/codex_oauth.py`: `create_5sim_phone_verifier`, `run_codex_oauth` add-phone handling.
- `core/num5sim.py`: `ActivationPool`, `PoolEntry`, 5sim purchase/reuse/check/finish functions.
- `core/account_store.py` at the initial baseline: canonical MySQL repository with the existing `accounts` JSON field, before phone-binding operations were added.
- `core/local_config.py`: `.env` settings map and secret writer.
- `webui/server.py` at the initial baseline: defaults exposed `fiveSimApiKey`; the completion now returns only its presence flag.
- `webui/static/index.html` at the initial baseline: Settings included a duplicate key input and Tools contained `fivesimPanel`; both placements have been retired.
- Existing tests: `tests/test_account_store.py`, `tests/test_webui_accounts.py`, `tests/test_local_config_env.py`; no dedicated 5sim tests were found.
- User conversation: independent SMS settings page, 5sim auto-SMS in Codex push, max 3 different accounts per number, record number per account, OTP acceptance consumes slot, skip failed account, buy another after capacity, keep other provider paths available for future.
- Workspace limitation: `docs/current/AEGIS_MINIMALITY_REFERENCE.md` is absent; baseline governance and existing plans above are local authority candidates.

## Notes on acceptance choice

The exact persistence format uses account-data field `codexPhoneNumber`; display masks it as `••••<last4>`. Binding is permanent in this task; no unbind UI or automatic rollback. New 5sim purchases have a per-account bounded maximum of 3 orders; no global spend/budget control is added.

## Plan drift stop

If atomic distinct-account claim requires a new table, schema migration, external store, or broad serialization lock that would materially impact normal MySQL account writes, do not implement around it silently. Pause and ask the user to approve the minimal persistence-contract design.

## Handoff

Implementation is complete. Verification commands and reusable fixtures live under `tests/`; real external paid/authentication flows remain unverified. If MySQL loses the connection after an external OTP is accepted, the operator must reconcile the account and order before retrying, since that external action cannot be rolled back by the database.

## Acceptance checklist

- [x] Separate 「短信设置」 nav/view and existing 5sim controls moved, not copied.
- [x] One `.env` key input, secure blank-retention behavior; no API response returns key.
- [x] Codex OAuth automatically invokes configured 5sim only when phone verification is required.
- [x] OTP acceptance binds masked-visible phone to that specific MySQL account.
- [x] At most 3 distinct account IDs per phone across concurrent Codex workers.
- [x] A failed account is not retried/assigned its temporary number during the batch; following account continues.
- [x] Saturated number is never reused; new order follows bounded buy-attempt rule.
- [x] Existing independent 5sim features and unrelated SMS providers remain intact.
- [x] Tests / compile / script syntax pass with no live third-party operation.
- [x] Plan results reflect actual evidence and limitations.

## Context completeness

This plan is self-contained for an engineer without prior chat context; the conversation remains the authoritative source for the user's approval and safety boundary.

## ADR signal

Persistent account field, account binding contract, and cross-account atomic cap introduce durable architecture decisions. Before implementation, determine whether an accepted ADR already owns this; none was found in the listed workspace. If architecture genuinely changes from current MySQL JSON owner, preserve approved alternatives and baseline-sync the parent Codex push / MySQL plan only after verified implementation. Do not create an ADR from this plan alone.

## Execution readiness

Intent lock: three distinct MySQL accounts per phone; OTP acceptance consumes binding; batch-failed account skips; cap causes new number; no live paid side effects.

Scope fence: Codex OAuth + MySQL binding + existing 5sim configuration UI/owner + account list display; other SMS provider implementations out of scope.

Baseline lock: use account store and registered 5sim adapter; no second source of truth, no new table without renewed approval, preserve unrelated dirty tree.

Drift stop: unable to guarantee atomic limit inside existing schema; need a new provider framework or endpoint; default secret disclosure; unbounded purchase; any need to write real environment credentials or perform external side effects.

Handoff criteria: execution may proceed task-by-task; verification is fixtures-only and any live Codex/5sim action remains separately unapproved.

## Artifact status

The accepted implementation and local verification are complete. This artifact is the execution record, including the initial baseline, acceptance evidence and the external-service limitations in Results. It does not claim that real paid SMS or live authentication has been exercised.

## Explicit constraints

- Never store SMS OTP in MySQL or output logs.
- Bind only when DOM/page confirms the phone verification step has completed, not merely after code retrieval or input fill.
- Refuse to reuse a number with three account records, even if the 5sim server's reuse endpoint offers it.
- A number that OTP accepted but account persistence fails is an indeterminate claim: fail closed and never silently continue as though it were unbound.
- Only account rows from selected Codex push targets can be updated; use immutable numeric MySQL IDs as binding keys.
- Existing general API fields should return `fiveSimApiKeyPresent`, not secret `fiveSimApiKey`; this is an identified current exposure to fix in the same configuration owner slice.
- Verify project process state before any service restart; this plan does not request restart.

## Derived behavior examples

- Accounts A and B OTP accepted on number N -> N has two distinct account owners and can be reused for C.
- C OTP accepted -> N full; fourth account D must buy a fresh number.
- B is retried after its later OAuth failure -> B is already bound; batch retry should not use N (user said failed account skipped), and N remains at two distinct owners, not three.
- Same account ID tries to claim N twice due to callback retry -> idempotent write, still one distinct owner.
- A receives OTP but page refuses it -> no association; order can be cancelled and number is not counted.
- Network exception after OTP accepted but before SQL confirmation -> workflow fails closed with reconciliation-needed status; do not assign N to another account without authoritative binding lookup.

## Definition of done

All acceptance checklist items have test evidence, all changed files are reviewed against unrelated dirty state, full tests and syntax checks pass, docs updated; external paid integration stays explicitly unverified.
