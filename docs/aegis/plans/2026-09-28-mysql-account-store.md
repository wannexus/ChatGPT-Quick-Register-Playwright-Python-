# MySQL Account Store, Local Validity, and Cookie Refresh

## Intent and approved requirements

Replace `output/*.json` account persistence with MySQL as the single source of truth; add local-only account validity checks in the Accounts view; support selected-account re-login that refreshes the OpenAI/ChatGPT session and persists it; encrypt saved account passwords at application level using a key in ignored `.env`; and remove the existing account JSON files only after the database and schema are initialized and verified. The user explicitly authorized permanent deletion of all `output/*.json`; do not remove other output files.

Remote token validation is out of scope. Local validity means only that locally stored token/session/expiry fields are present and internally consistent; it does not prove that a remote service accepts a token. Re-login uses stored email/password when available and the configured QQ IMAP/manual code flow. Existing browser/profile, account-specific login, task confirmations, and unrelated tool behavior should otherwise remain intact.

## Baseline and current owners

- Project root: `/Users/zsl/Desktop/free`; baseline commit `4e80b7a` with numerous pre-existing modified/untracked files. Preserve unrelated changes.
- Existing test baseline: `./.venv/bin/python -m unittest discover -s tests -v` passes 10 tests.
- MySQL server is running locally; `mysql -h 127.0.0.1 -u root ... SELECT VERSION(), CURRENT_USER()` succeeded read-only. MySQL database does not yet exist. Python packages `pymysql` and `cryptography` are absent from the current venv and must be declared/installed.
- Account writes currently originate in `core/session.py` / `register.py`; account CRUD, AC checks, and PAY.153 result writes live in `webui/server.py`; SUB2API conversion currently reads directories in `core/sub2api.py`; the Accounts view is in `webui/static/index.html`.
- The project has no existing MySQL/account repository or prior ADR/context baseline. `core/local_config.py` owns other local app settings, not account persistence. `core/flow.py` owns browser login/cookie clearing, not durable account storage.
- Existing `output/*.json` account files are sensitive and explicitly authorized for deletion after the DB gate. Preserve `output/debug`, 5sim number-pool JSON, logs, and all other non-account output.

## First-principles / architecture decision

- **Invariant:** one durable owner for account state; successful registration/re-login, account CRUD, account list/detail, AC metadata and PAY.153 metadata must read/write the same MySQL record.
- **New surface:** add `core/account_store.py` as the single MySQL connection/schema/repository and password-encryption owner. Existing `core/session.py` remains session acquisition/snapshot construction; `register.py` orchestrates and calls the repository. Existing `core.sub2api` remains transformation/network client, changed to accept in-memory account records instead of owning directory discovery.
- **Creation proof:** current local-config, session, browser-flow, and SUB2API modules do not provide a canonical persistent account repository; duplicating JSON plus MySQL would create split-brain state. A dedicated repository is the narrowest single owner.
- **Preserve:** business fields and current UI/tool interactions, per-account account controls, AC/PAY task results, explicit confirmation before third-party token sending, and non-account files under `output/`.
- **Retire:** account JSON persistence/lookup, filename-path traversal handling, JSON-based relogin-required index, and SUB2API's account-directory enumeration. Do not retain a silent JSON fallback or dual-write path.
- **Data migration:** intentionally none. The user chose to delete all old account JSON rather than import it. New DB starts empty.
- **Known residual risk:** password ciphertext is protected by the Fernet key; account JSON stored in MySQL still contains session and OAuth tokens unless the implementation encrypts the entire payload. Limit DB grants and keep credentials local; never expose the stored password from WebUI APIs. Do not log tokens, decrypted passwords, or the encryption key.

## Scope and contracts

1. **Configuration and persistence:** load `.env`; use `QR_MYSQL_HOST`, `QR_MYSQL_PORT`, `QR_MYSQL_USER`, `QR_MYSQL_PASSWORD`, `QR_MYSQL_DATABASE`, and `QR_ACCOUNT_ENCRYPTION_KEY`. Add a documented `.env.example` and ignored local `.env` using local-server defaults/placeholders; never commit `.env`. Add a safe initializer that creates the named DB and accounts table, then offers a read-only readiness check. Use utf8mb4 and parameterized SQL. Table uses stable numeric account IDs, unique email, encrypted password, JSON account payload (password excluded), and timestamps. Fail closed when required config/key or DB is unavailable.
2. **Registration/re-login CLI:** new registrations persist to MySQL; do not write account JSON. Replace JSON scans for used emails and relogin selection with repository queries. Selected-email and marked-account re-login load the DB record, decrypt password only in process memory, use existing Playwright login/QQ code collection and cookie-clearing functions, update the same record on success, and update retry/error metadata on failure without deleting or clobbering the prior session. Retire file-based relogin routes/options when all internal consumers have been converted.
3. **WebUI/API and tools:** account list/detail/delete/cleanup/relogin-required operations use account IDs/repository; detail response redacts password. Add an explicit local validity-check API (single and batch), persist check timestamp/status or return computed status without network access, and return statuses for missing session/token, expired, unexpired, and unknown expiry. AC checks read/write MySQL metadata. PAY.153 task context stores an account ID and writes terminal result to that DB record. SUB2API export/push consumes DB account rows and keeps only the existing explicit export/push behavior.
4. **UI/docs/dependencies:** add local validity status and check button(s), and per-account “refresh Cookie/re-login” action with explicit confirmation and pending/result feedback. Label the check as local-only. Continue using the current shared API/UI field semantics where possible, but rename file-specific contracts when no longer meaningful. Update README, dependencies and `.env.example`; add no unrelated UI or endpoint.
5. **Authorized data retirement gate:** only after the DB exists, the account table/schema is verified, the application repository can connect and read/write a disposable test record safely, and the real account table is confirmed empty (no import was selected), remove only direct `output/*.json` files. Re-count before/after and verify all non-JSON output remains. If any condition fails, do not delete; report the exact blocker. Existing user confirmation covers this exact path/pattern and acknowledges irreversibility.

## Change necessity and execution route

- User-visible need: local validity query and per-account cookie/session refresh, with credentials and account state moved to MySQL.
- No-code option: documentation or a SQL export cannot provide working account CRUD/re-login or one canonical store.
- Minimum change: one repository owner; reroute current account writers/readers; add scoped UI/API actions; delete only confirmed old account files after the DB gate.
- Decision: code-change.
- Execution route: inline. The persistence boundary spans shared modules and requires ordered DB/data coordination; parallel writers would create conflicting ownership. User confirmation is not additionally required for code; actual deletion is already explicitly authorized but remains conditional on the exact DB gate.

## TDD route

- Mode: `auto`; decision: `strict` due persistence/source-of-truth/schema migration and credential handling.
- Authority: Aegis auto-route rules for persistence/migration risk; the user did not separately request a TDD process.
- Test posture: add focused failing tests first for Fernet encrypt/decrypt and fail-closed config, local validity states, repository parameterization/upsert/ID behavior with a fake DB connection, and API contract/redaction; then implement and refactor. Keep current ten tests green. Do not assert live external login or AC services in unit tests.
- Verification: unit tests, compileall, Python/inline JS syntax, API tests, local MySQL init/connect/read-write smoke test, and output-scope pre/post deletion check.

## Implementation tasks

1. Add `core/account_store.py`, MySQL/env/crypto dependencies, `.env.example` and local `.env` config; define initialization/readiness commands and focused tests.
2. Refactor registration/session snapshot and CLI to persist account data to repository; switch email de-duplication, account re-login selection, AC batch persistence, and re-login status updates to MySQL. Add selected-account re-login by ID.
3. Refactor WebUI account CRUD/list/detail, local validity endpoint, AC check, PAY.153 result storage, and SUB2API export/push to DB account IDs/records. Ensure detail and logs never return password plaintext.
4. Add Accounts table local validity column/check control and per-account confirmed re-login/Cookie refresh UX; refresh UI state on task completion; update README/setup docs.
5. Run all regression checks; initialize and verify the local MySQL database/schema and repository read/write. Confirm new database has zero accounts (legacy JSON is intentionally not imported). Only then remove exact `output/*.json`; verify count zero and preserve all non-account output.

## Acceptance and stop conditions

- No internal account CRUD or tool path treats `output/*.json` as a source of truth; no account JSON is newly written.
- Password is encrypted at rest and never returned by account detail/list APIs; missing/invalid key fails closed.
- Accounts UI clearly distinguishes local metadata checks from remote validity, reports a check timestamp, and a one-account relogin refreshes only that DB record after a successful session capture.
- Existing AC/PAY/SUB2API functions still consume/update the same DB-owned account state; no duplicate persistence owner is added.
- MySQL DDL/readiness tests pass before the deletion gate. If local credentials, grants, driver installation, schema, or DB transaction validation fail, retain every legacy JSON and leave the task explicitly incomplete at the destructive-cleanup step.
- No change to unrelated local dirty files, other domains' browser storage, or non-account files under `output/`.

## Self-review

The approved feature scope maps to the tasks and checks. DB persistence is canonical rather than dual-written. Internal JSON consumers are enumerated and retired; debug and 5sim files have independent legitimate roles and are retained. The user explicitly chose permanent deletion instead of import and approved the exact `output/*.json` scope; this plan still prevents deletion until DB/schema/read-write verification succeeds. External consumers of the old account JSON files are unobservable; existing README claims and internal SUB2API consumer are being updated/migrated. Local MySQL root access is currently available read-only, but package installation and schema creation are not yet verified. Token-bearing account JSON remains unencrypted under the accepted password-only encryption design; this is called out as residual risk, not silently claimed protected.

## Phone-binding contract follow-up

`accounts.account_data.codexPhoneNumber` is owned by `AccountStore.reserve_codex_phone` and its accepted-OTP commit callback; verified idempotent writes use `bind_codex_phone`. Normal account upserts preserve this stored field and cannot create/replace a binding from a snapshot. Account/phone MySQL advisory locks enforce capacity without a new table; no row transaction is held while waiting for SMS. The SMS plan contains the completion evidence and external failure limitations. The local SQL smoke test uses only a connection-local temporary table and does not modify production accounts or persistent schema.
