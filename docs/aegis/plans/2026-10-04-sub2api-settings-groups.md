# SUB2API 设置：分组动态下拉与中文标签

## Task Intent Draft

- Outcome：在 SUB2API 设置弹窗打开时，使用已保存到 `.env` 的服务 URL 和管理员 API Key 自动查询分组，并让用户从分组下拉框选择；若用户在弹窗里修改 URL 或 Key，也自动查询新目标。并将 `concurrency`、`priority`、`rate_multiplier`、`privacy_mode` 等设置标签改为中文。
- Scope：账号页面 `webui/static/index.html`；新增一个只读的 SUB2API 分组查询 API，复用 `core/sub2api.py` 的管理员鉴权和分组列表函数。
- Non-goals：不修改 SUB2API 下游字段、创建账号请求契约、`.env` 键名、旧 JWT 兜底鉴权、不增加新的持久化数据。
- Acceptance：打开设置时先加载 defaults，再用 `.env` 中的 URL/Key 自动查询分组；URL / Key 变更后防抖自动查询；新请求作废旧响应；成功将分组名填入下拉框且保留已保存选择；无效 URL/Key、空列表、网络或接口错误显示中文状态；Key 不回显、不写前端存储/日志；设置里的参数标签中文化，实际 payload 仍发送既有字段和值。
- Stop condition：UI 可查询、选择、保存，回归测试通过；不执行真实账号推送。

## Baseline Read Set

- `webui/static/index.html`：设置 modal、`s2SettingsPayload`、`applyS2Defaults`、`loadS2Defaults`、`saveS2Settings`。
- `webui/server.py`：`defaults` / `LocalConfigPayload`，新增只读分组查询路由；不得向 defaults 响应回传 API Key。
- `core/sub2api.py`：`Sub2ApiTarget`、`auth_headers`、`list_groups`，作为认证和 SUB2API 请求的规范 owner。
- `tests/test_webui_accounts.py`、`tests/test_sub2api_admin_key.py`：API 与鉴权测试。
- `README.md`：SUB2API 设置描述。

## Design

1. 新增 `POST /api/sub2api/groups`，载荷为 base URL、管理员 API Key，以及仅当未提供 Key 时才用到的 JWT fallback 邮箱/密码。路由读取当前保存的密钥作留空沿用；创建 `Sub2ApiTarget` 后复用 `auth_headers()` 和 `list_groups()`。响应只返回可选分组（id/name 等必要标识），绝不返回请求凭据；错误通过现有中文异常处理。
2. 前端对 URL / Key 的输入变化做短防抖自动请求；每次请求递增序号，忽略旧序号响应；仅成功后更新 `<select>`。若当前已保存分组仍在返回项中则保留，否则选首个分组（若存在）；若空列表则禁用选择并显示状态。
3. 管理员 API Key 输入为空时按当前产品契约沿用已保存 Key。由于服务端不会把 Key 交给前端，查询 API 内部从 `.env`/有效配置读取保存值；临时输入的 Key 只在本次请求里传递。
4. 设置字段中文标签建议：分组、隐私模式、并发数、优先级、速率倍率；`training_off/on` 保留为 option value，显示「关闭训练（推荐）/允许训练」。参数值和后端字段保持兼容。
5. 不改变 SUB2API 导入/推送行为，不在页面初始加载时自动请求或保存配置；打开设置弹窗时先加载 defaults，再立即用已保存 URL / Key 获取分组；弹窗内 URL / Key 改动后也触发防抖查询。

## High-risk counterexamples / boundaries

- 切换 URL / Key 后旧请求晚返回，不能用过期分组覆盖新目标。
- Key 未输入但已有保存值时应能获取分组；页面与响应不得暴露这个已保存 Key。
- 无 Key 且无可用 JWT 凭据要返回可读错误，不能发匿名管理员请求。
- 已保存分组名称不在新目标的组列表时，必须清晰显示新选择，不应悄悄把保存状态误认为旧组仍有效。
- 后端仍按现有 `group_name` 字符串解析组 ID；设置下拉提交名称而不是 ID，避免改变已有持久化格式。

## Approval

用户批准：打开 SUB2API 设置弹窗时，使用已保存到 `.env` 的 URL / Key 自动获取分组；弹窗内修改 URL / Key 时也自动刷新。批准日期：2026-10-04。

## Implementation Notes (after approval)

- API owner: `webui/server.py` 只做 payload 校验、配置合并和路由响应；SUB2API HTTP 及鉴权逻辑复用 `core/sub2api.py`，不复制 HTTP 实现。
- UI owner: `webui/static/index.html` 当前设置组件内完成防抖和响应序号控制；不加全局请求层或新的 owner。
- Compatibility: `s2GroupName` / `QR_SUB2API_GROUP_NAME` 保持不变；`core.sub2api.Sub2ApiTarget.group_name` 保持不变。
- Verification: 单测覆盖管理员 Key header、已保存 Key 沿用、JWT 兜底、无凭据/服务错误、secret 不出响应；页面脚本语法检查及 UI fixture 覆盖防抖、旧响应忽略、空分组和保存值回填。

## TDD Route

- Mode: auto
- Decision: strict
- Strict authority: recorded auto decision for producer/consumer contract, credential handling, and UI behavior change.
- Test posture: strict RED / GREEN / REFACTOR at server API and UI behavior seams.
- Reason: new authenticated API contract plus browser-side async behavior and saved-secret use have meaningful regression/security risk.
- Verification: API tests for saved/transient credentials and secret non-disclosure; UI tests for open-time load, debounce/stale response, selection preservation, errors; related existing account/config/Sub2API suites.

## Implementation Plan

1. **Server read-only groups API** — in `webui/server.py`, add a Pydantic request model and `POST /api/sub2api/groups`. Merge submitted URL/key/email/password with `effective_config()` using the established blank-secret-means-keep rule. Construct `sub2api.Sub2ApiTarget`, call `sub2api.auth_headers()` and `sub2api.list_groups()`, return sanitized `{id, name}` only. Keep all secrets out of response, logs, and error serialization. Add tests for admin key, saved key, JWT fallback, absent target/credentials, transport/API error, and secret non-disclosure.
2. **Settings modal behavior and labels** — in `webui/static/index.html`, replace free-text group field with select + status; open modal by loading defaults, then invoke group refresh. Add debounce on URL/key edits, monotonic request id to discard stale results, preserve saved group if present, and show errors/empty results in Chinese. Translate privacy/concurrency/priority/rate multiplier and dry-run labels while retaining all option values and payload field names. Keep the API Key input blank and masked; no localStorage or console logging.
3. **Documentation and verification** — update `README.md`; run focused server and SUB2API tests, full test suite if feasible, `py_compile`, extracted script `node --check`, and browser fixture/manual QA against mocked fetch only. Do not run a live push or write account data.

## Retirement / compatibility

- Retire only the free-text group-name control; replace it with a dynamic select while preserving persisted `s2GroupName` string.
- Retain core `group_name`, `QR_SUB2API_GROUP_NAME`, `training_off/on` values, JWT fallback, API-key priority, and existing push transport unchanged.
- New endpoint is read-only and does not create accounts; it is the single server bridge from UI to canonical `core/sub2api` auth/list_groups owners.

## Self-review

- Scope is limited to one UI surface and its server read endpoint; no new persistence owner or sub2api HTTP adapter.
- Secret continuity uses effective `.env` config server-side; saved key is never sent down to browser.
- Async race handling is explicit and bounded to this modal; inputs do not trigger calls until complete values are available after debounce.
- Verification is fixture-only for external SUB2API; live credentials and service behavior remain outside this task.

## Execution Route

- Decision: inline implementation (the API and UI are sequentially dependent and owned in this one task; delegation adds coordination without useful parallelism).
- User confirmation required: no — design approved, no live push or destructive action is in scope.

## Results

- 已实现 `POST /api/sub2api/groups`：从临时表单值优先、否则从有效 `.env` 配置取得 URL / Key / JWT 凭据；复用 `core.sub2api.auth_headers()` 和 `list_groups()`；响应仅含组 `id/name`；保存或临时凭据均不在错误响应回显。
- 设置弹窗会先取 defaults 再查分组；URL / Key 编辑后 450ms 防抖刷新；单调 request id 与关闭弹窗作废逻辑忽略旧响应；保存的组名仍存在时保留，否则选首项。动态选项用 DOM `textContent` 构建。
- 标签更新为中文，保留 `s2GroupName`、后端字段和值契约；README 已说明自动获取。
- 验证：账号 API 与 SUB2API 回归套件 54 tests 通过；全量 `unittest discover -s tests` 297 tests 通过；`py_compile` 与内联 JS `node --check` 均通过。全量测试期间有既存 `RuntimeWarning`（一项 async 设置覆盖测试未 await）及 asyncio `ResourceWarning`，但测试均成功。
- 未执行真实账号推送；当前批任务运行状态/生产界面未做触碰或重启。
