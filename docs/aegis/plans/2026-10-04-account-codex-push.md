# Account Codex Login And Push

Approved: retire marked-account relogin/deletion and old SUB2API export/push UI/API. Preserve account records, per-account Cookie refresh, local validity checks and ordinary deletion. Add selected-account fresh Codex login -> MySQL save -> SUB2API push, with truthful per-account status and no stale-token fallback.

TDD Route strict: inherited recorded authority, auto mode, persistence/contract changes. RED tests for selection, freshness, save-before-push, failures, identity mismatch, cancellation, config secrecy, task exclusion and retirement.

Owners: register.py owns the browser/login/OAuth adapter and gains a `codex-push` CLI mode; core/sub2api.py stays the transport owner; webui/server.py retires old routes and adds the new start API plus structured progress parsing; core/local_config.py and .env hold SUB2API settings; index.html replaces the removed controls with selection + target dialog.

Known failure fixed this round: earlier write calls were rejected because an empty `justification` was sent together with `sandbox_permissions`; omit both fields.

Verification: full unittest suite, compile checks, Playwright desktop/mobile checks with intercepted fixtures, no live .env edits, no live account deletion, restart the existing 8765 service and confirm HTTP 200.

Live external OAuth/push needs real credentials and is not proven by mocked tests. Remote create may succeed just before cancellation or network failure: report uncertainty, never retry automatically. No replacement DSH server.

## Result (verified)
- Retired: account-page 「重登标记账号」/「删除重登账号」, `POST /api/accounts/relogin`, `POST /api/accounts/delete-relogin-required`, and `POST /api/sub2api/{preview,export,push}` (now 404/405, absent from the route table).
- Added: `register.py --mode codex-push` (`--push-id`, `--s2-*`), `POST /api/accounts/codex-push`, `GET /api/accounts/codex-push/status`, and the account-page selection + target dialog. Settings live in `.env` as `QR_SUB2API_*`.
- Freshness contract: a Codex OAuth result without `access_token` is a hard failure; the stored `codexAuth` is never reused as a push source; fresh credentials are written to MySQL before the remote create; a failed login re-marks the account, a push failure keeps the fresh credentials.
- Evidence: 227 unittests OK; `py_compile` OK; `node --check` OK; 33/33 Playwright checks PASS (selection, dialog, request body carries only ticked IDs, per-account 登录/OAuth/保存/推送 rows, table badge, password never echoed, narrow viewport); real account page renders 13 columns / 3 accounts with no console errors; 8765 service restarted (PID 37706) and returns 200.
- Bug found by browser verification: `codexPushTimer` was undeclared, so the ReferenceError was swallowed and progress polling never repeated. Fixed and re-verified.
- Deliberately retained: `--relogin-marked` CLI flag, `core/sub2api.build_export_bundle`, and per-account 「刷新 Cookie」; none are reachable from the retired account-page controls.
- Not verified (needs real external credentials): an actual ChatGPT login + live SUB2API create.

## Result — admin API key auth (upstream contract)
- Reference: the upstream [Wei-Shaw/sub2api admin skill](https://raw.githubusercontent.com/Wei-Shaw/sub2api/main/skills/sub2api-admin/SKILL.md) — `x-api-key: <admin api key>` first, `Authorization: Bearer <jwt>` only as fallback, `INVALID_ADMIN_KEY` means regenerate.
- `core/sub2api.py`: `Sub2ApiTarget.admin_api_key`; `auth_headers()` returns `x-api-key` and never logs in when a key exists; `email`/`password` became optional JWT fallback; `_request_json` takes explicit headers; `INVALID_ADMIN_KEY` is translated into an actionable message; push progress stages renamed login/login_ok -> auth/auth_ok with the mode.
- Config: `QR_SUB2API_ADMIN_API_KEY` added; the email/password pair is kept only as a documented fallback. The settings dialog leads with the API key and hides the fallback behind a `<details>`.
- Secrecy: the key is redacted from the echoed command, never returned by `/api/defaults` (only `s2AdminApiKeySet`), and left out of argv entirely once persisted in `.env`.
- Evidence: 241 unittests OK (incl. 6 new `x-api-key` contract tests + key-only/JWT-fallback target tests); `py_compile` and `node --check` OK; 38/38 Playwright checks PASS; temporary port-8799 instance verified the new API without disturbing the operator's running task; 8765 restarted (PID 42621) — page 200, retired routes 404, key-based validation confirmed.
- Incident: one shell check accidentally launched a real `codex-push` CLI run against a valid target. It was killed immediately, before completing a login; a re-login task started from the web UI ran concurrently and finished on its own. CLI checks are now limited to paths that fail before the browser opens.

## Result — ban detection (mailbox polling)
- `core/ban_check.py` (new): strong-phrase classification (EN + zh), RFC822 -> plain record, recipient-matched `find_banned_accounts`. Ordinary verification mails and "new login" alerts stay clean by design.
- `core/qq_imap.py:scan_qq_messages` / `core/mhjc.py:scan_mhjc_messages`: read recent messages without code filtering; one QQ pass covers duck/iCloud forwards, each MHJC account uses its stored mailbox password.
- `register.py --mode ban-scan` (`--ban-id`, `--ban-since-days`, `--ban-dry-run`): marks `banned`/`banReason`/`banSubject`/`banSource`/`banDetectedAt` on the same MySQL row, clears `reloginRequired`, never deletes and never auto-clears; emits `[ban-scan-result]` lines.
- Login-time detection: `flow.AccountBannedError` from the deactivation page; both `relogin` and `codex-push` mark the account instead of retrying, and a banned account is refused before any browser work in `codex-push`.
- Web: `POST /api/accounts/ban-scan` + `GET /api/accounts/ban-scan/status`, a 封禁 column/badge with the reason, a 检测封禁 button, and a banned counter in the stats line.
- Evidence: 290 unittests OK; **real dry-run scan against the three live MHJC mailboxes read 5 messages with 0 false positives**; 12/12 ban UI Playwright checks PASS; real page renders 14 columns with no console errors; 8765 restarted (PID 45299) — page 200, ban endpoints live, `banned=false` on all three real accounts before any marking.

## Result — re-login / refresh reliability
- Root cause proven from the operator's log: after the email submit the flow sat on `auth.openai.com/api/accounts/authorize?...`, which matches none of the three URL patterns, so healthy accounts waited out 25 s x 3 attempts — the password/OTP form is rendered **on that same URL**.
- Fix: `flow.classify_login_signal` + `wait_for_login_step` decide from the DOM (password/OTP input visible) with the URL only as a fallback; the OTP-vs-password branch is now DOM-driven too; first-step budget 25 s -> 30–60 s derived from `--relogin-timeout`; email resubmit only when still on the email form; password refill preserved.
- Failure reasons are now actionable: banned / Cloudflare / rate-limited instead of a bare timeout.
- Evidence: `tests/test_login_signal.py` (15 tests) includes the regression where the code form is reached while `page.url` never leaves the authorize endpoint.

## Result - automatic phone verification

- Codex push now passes the selected MySQL account ID/store to the existing 5sim adapter. Account/phone advisory locks cover verification, and accepted OTP commits `codexPhoneNumber` immediately. Later OAuth/push failure keeps that binding; ordinary saves cannot overwrite it.
- The SMS plan records 332 passing unit tests, 18 passing mocked UI checks, real DOM OAuth fixtures, isolated MySQL temporary-table SQL checks and the remaining external-service limitations. No live paid order or external account change was made in this completion audit.
