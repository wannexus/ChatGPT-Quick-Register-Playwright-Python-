"""FastAPI server for the ChatGPT Quick Register web UI.

启动：
    python3 webui/server.py
默认监听 http://127.0.0.1:8765/
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import ac_checker, num5sim, pay153, qq_imap, sub2api  # noqa: E402
from core.account_store import AccountStore, check_local_validity  # noqa: E402
from core.http_utils import build_proxy_url, proxy_parts, redact_proxy_url  # noqa: E402
from core import proxy_test as proxy_test_module  # noqa: E402
from core import mhjc as mhjc_module  # noqa: E402


def mhjc_api_base() -> str:
    return mhjc_module.DEFAULT_API_BASE
from core import local_config  # noqa: E402  (live .env path)
from core.local_config import effective_config, save_config  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

SENSITIVE_ARG_NAMES = {
    "--password",
    "--duck-token",
    "--qq-pass",
    "--mhjc-api-key",
    "--proxy",          # a full proxy URL may embed user:password
    "--proxy-pass",
    "--s2-admin-password",
    "--s2-admin-api-key",
}


def _saved_flag_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _proxy_round_trippable(proxy_value: str) -> str:
    """Return the stored proxy URL only when it carries no credentials.

    A redacted URL must never be sent back for editing: if the browser echoed
    `user:***@host` into the config, the literal `***` would become the stored
    password. When credentials exist the UI edits the separate fields instead.
    """
    parts = proxy_parts(proxy_value)
    if parts["host"] and not (parts["user"] or parts["password"]):
        return proxy_value
    return ""


def _cfg_proxy_insecure(cfg: dict[str, Any]) -> bool:
    return str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Batch process manager (single concurrent run)
# ---------------------------------------------------------------------------
def _redact_args(args: List[str]) -> List[str]:
    redacted: List[str] = []
    hide_next = False
    for arg in args:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        if arg in SENSITIVE_ARG_NAMES:
            redacted.append(arg)
            hide_next = True
            continue
        redacted.append(arg)
    return redacted


class BatchManager:
    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.subscribers: List["asyncio.Queue[str]"] = []
        self.recent: List[str] = []  # ring buffer of latest log lines for new subscribers
        self._reader_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, args: List[str]) -> "tuple[bool, str]":
        async with self._lock:
            if self.is_running:
                return False, "已有任务在运行"
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            cmd = [sys.executable, "register.py", *args]
            visible_cmd = [sys.executable, "register.py", *_redact_args(args)]
            self.recent = [f"$ {' '.join(visible_cmd)}"]
            await self._broadcast(self.recent[0])
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(PROJECT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            return True, "started"

    async def _read_loop(self) -> None:
        assert self.proc is not None
        try:
            while True:
                if self.proc.stdout is None:
                    break
                line = await self.proc.stdout.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="replace").rstrip()
                self.recent.append(msg)
                if len(self.recent) > 2000:
                    self.recent = self.recent[-1000:]
                await self._broadcast(msg)
        finally:
            rc = await self.proc.wait()
            if rc is not None and rc < 0:
                tail = f"<<< 任务被信号终止 (signal {-rc}) >>>"
            else:
                tail = f"<<< 任务结束 (exit code {rc}) >>>"
            self.recent.append(tail)
            await self._broadcast(tail)

    async def _broadcast(self, msg: str) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def subscribe(self) -> "asyncio.Queue[str]":
        q: "asyncio.Queue[str]" = asyncio.Queue()
        for line in self.recent[-500:]:
            q.put_nowait(line)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[str]") -> None:
        with contextlib.suppress(ValueError):
            self.subscribers.remove(q)

    async def stop(self) -> bool:
        if not self.is_running or self.proc is None:
            return False
        with contextlib.suppress(Exception):
            self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                self.proc.kill()
            await self.proc.wait()
        return True


manager = BatchManager()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="ChatGPT Quick Register UI")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _sanitize(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")
    return cleaned or "unknown"


_RECORD_DERIVED_KEYS = frozenset({
    "id", "password", "localValidity", "passwordStored", "createdAt", "updatedAt",
})

# Credentials that must never leave the process through an API response.
_REDACTED_RESPONSE_KEYS = ("password", "emailPassword")


def _account_record_payload(account: dict[str, Any]) -> dict[str, Any]:
    """Strip repository-owned fields and stale relogin markers before re-saving a record."""
    return {
        key: value
        for key, value in account.items()
        if key not in _RECORD_DERIVED_KEYS and not key.startswith("relogin")
    }


def _account_summary(account: dict[str, Any]) -> dict[str, Any]:
    session = account.get("session") if isinstance(account.get("session"), dict) else {}
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    ac_check = account.get("acCheck") if isinstance(account.get("acCheck"), dict) else {}
    return {
        "id": int(account["id"]),
        "email": account.get("email") or "",
        "savedAt": account.get("savedAt"),
        "sessionFetchOk": bool(account.get("sessionFetchOk")),
        "expires": session.get("expires"),
        "hasAccessToken": bool(session.get("accessToken")),
        "hasRefreshToken": bool(account.get("refresh_token") or (account.get("codexAuth") or {}).get("refresh_token")),
        "userEmail": user.get("email"),
        "authMode": account.get("authMode"),
        "codexPhoneNumberMasked": (
            f"••••{str(account.get('codexPhoneNumber') or '')[-4:]}"
            if account.get("codexPhoneNumber") else ""
        ),
        "emailSource": account.get("emailSource"),
        "codeSource": account.get("codeSource"),
        "reloginRequired": bool(account.get("reloginRequired")),
        "reloginReason": account.get("reloginReason") or "",
        "banned": bool(account.get("banned")),
        "banReason": account.get("banReason") or "",
        "banSubject": account.get("banSubject") or "",
        "banSource": account.get("banSource") or "",
        "banDetectedAt": account.get("banDetectedAt"),
        "payurlCheck": account.get("payurlCheck") or {},
        "acCheck": ac_check,
        "localValidity": account.get("localValidity") or {},
        "passwordStored": bool(account.get("passwordStored")),
        "createdAt": account.get("createdAt"),
        "updatedAt": account.get("updatedAt"),
    }


# ---------------- HTML ----------------
@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------- Defaults / config ----------------
@app.get("/api/defaults")
async def defaults():
    """前端首次加载时自动填充：环境变量优先，本地配置文件兜底。"""
    cfg = effective_config()
    return {
        "duckTokenPresent": bool(cfg.get("duckToken")),
        "mhjcApiKeyPresent": bool(cfg.get("mhjcApiKey")),
        "mhjcUsername": cfg.get("mhjcUsername") or "",
        "mhjcApiBase": cfg.get("mhjcApiBase") or mhjc_api_base(),
        "qqUserPresent": bool(cfg.get("qqUser")),
        "qqPassPresent": bool(cfg.get("qqPass")),
        "fiveSimApiKeyPresent": bool(cfg.get("fiveSimApiKey")),
        "duckToken": cfg.get("duckToken") or "",
        "duckBaseUrl": cfg.get("duckBaseUrl") or "",
        "icloudHost": cfg.get("icloudHost") or "",
        "icloudFetchMode": cfg.get("icloudFetchMode") or "",
        "qqUser": cfg.get("qqUser") or "",
        "qqPass": cfg.get("qqPass") or "",
        "qqMaxAttempts": cfg.get("qqMaxAttempts") or "",
        "qqInterval": cfg.get("qqInterval") or "",
        "icloudLoginTimeout": cfg.get("icloudLoginTimeout") or "",
        "fiveSimCountry": cfg.get("fiveSimCountry") or "any",
        "fiveSimOperator": cfg.get("fiveSimOperator") or "any",
        "fiveSimProduct": cfg.get("fiveSimProduct") or "openai",
        "fiveSimMaxPrice": cfg.get("fiveSimMaxPrice") or "",
        "fiveSimAcquirePriority": cfg.get("fiveSimAcquirePriority") or "rate",
        "fiveSimCandidateLimit": cfg.get("fiveSimCandidateLimit") or "8",
        "acCheckerBaseUrl": cfg.get("acCheckerBaseUrl") or ac_checker.DEFAULT_BASE_URL,
        "acCheckerPromoId": cfg.get("acCheckerPromoId") or ac_checker.DEFAULT_PROMO_ID,
        "pay153BaseUrl": cfg.get("pay153BaseUrl") or pay153.DEFAULT_BASE_URL,
        "proxy": _proxy_round_trippable(cfg.get("proxy") or ""),
        "proxyDisplay": redact_proxy_url(cfg.get("proxy") or ""),
        "proxyScheme": cfg.get("proxyScheme") or "",
        "proxyHost": cfg.get("proxyHost") or "",
        "proxyPort": cfg.get("proxyPort") or "",
        "proxyUser": cfg.get("proxyUser") or "",
        "proxyPasswordSet": bool(cfg.get("proxyPassword")),
        "proxyEnabled": _saved_flag_value(cfg.get("proxyEnabled", "1")),
        "proxyInsecure": _cfg_proxy_insecure(cfg),
        "emailSource": cfg.get("emailSource") or "",
        "codeSource": cfg.get("codeSource") or "",
        "authMode": cfg.get("authMode") or "",
        "count": cfg.get("count") or "",
        "cooldown": cfg.get("cooldown") or "",
        "headless": _saved_flag_value(cfg.get("headless")),
        "noPersistent": _saved_flag_value(cfg.get("noPersistent")),
        "noClearTokens": _saved_flag_value(cfg.get("noClearTokens")),
        "stopOnError": _saved_flag_value(cfg.get("stopOnError")),
        "runCodexOauth": _saved_flag_value(cfg.get("runCodexOauth")),
        "s2BaseUrl": cfg.get("s2BaseUrl") or "",
        "s2AdminApiKeySet": bool(cfg.get("s2AdminApiKey")),
        "s2AdminEmail": cfg.get("s2AdminEmail") or "",
        "s2AdminPasswordSet": bool(cfg.get("s2AdminPassword")),
        "s2GroupName": cfg.get("s2GroupName") or "codex",
        "s2Concurrency": cfg.get("s2Concurrency") or "5",
        "s2Priority": cfg.get("s2Priority") or "1",
        "s2RateMultiplier": cfg.get("s2RateMultiplier") or "1",
        "s2PrivacyMode": cfg.get("s2PrivacyMode") or "training_off",
        "envPath": str(local_config.ENV_PATH),
    }


class LocalConfigPayload(BaseModel):
    # `forbid` so a newly added UI field can never be silently dropped again.
    model_config = {"extra": "forbid"}

    duckToken: str = ""
    duckBaseUrl: str = ""
    icloudHost: str = ""
    icloudFetchMode: str = ""
    mhjcApiKey: str = ""
    mhjcApiBase: str = ""
    mhjcUsername: str = ""
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None
    icloudLoginTimeout: Optional[int] = None
    fiveSimApiKey: str = ""
    fiveSimCountry: str = "any"
    fiveSimOperator: str = "any"
    fiveSimProduct: str = "openai"
    fiveSimMaxPrice: str = ""
    fiveSimAcquirePriority: str = "rate"
    fiveSimCandidateLimit: Optional[int] = 8
    acCheckerBaseUrl: str = ""
    acCheckerPromoId: str = ""
    pay153BaseUrl: str = ""
    proxy: str = ""
    proxyScheme: str = ""
    proxyHost: str = ""
    proxyPort: str = ""
    proxyUser: str = ""
    proxyPassword: str = ""
    proxyEnabled: bool = True
    proxyInsecure: bool = False
    emailSource: str = ""
    codeSource: str = ""
    authMode: str = ""
    count: Optional[int] = None
    cooldown: Optional[int] = None
    headless: bool = False
    noPersistent: bool = False
    noClearTokens: bool = False
    stopOnError: bool = False
    runCodexOauth: bool = False
    s2BaseUrl: str = ""
    s2AdminApiKey: str = ""
    s2AdminEmail: str = ""
    s2AdminPassword: str = ""
    s2GroupName: str = ""
    s2Concurrency: Optional[int] = None
    s2Priority: Optional[int] = None
    s2RateMultiplier: Optional[int] = None
    s2PrivacyMode: str = ""


class Sub2ApiGroupsPayload(BaseModel):
    baseUrl: str = ""
    adminApiKey: str = ""
    adminEmail: str = ""
    adminPassword: str = ""


class ProxyTestPayload(BaseModel):
    proxy: str = ""
    proxyScheme: str = ""
    proxyHost: str = ""
    proxyPort: str = ""
    proxyUser: str = ""
    proxyPass: str = ""
    proxyEnabled: bool = True
    proxyInsecure: bool = False


def _effective_proxy_url(payload: ProxyTestPayload) -> str:
    """Compose the proxy URL the form currently describes.

    A blank password field means "keep the saved one" everywhere else in the UI,
    so the test must use the saved secret too, otherwise it would test a proxy
    that no real run would ever use.
    """
    saved = effective_config()
    host = payload.proxyHost.strip()
    if not host:
        return payload.proxy.strip()
    return build_proxy_url(
        host=host,
        port=payload.proxyPort,
        user=payload.proxyUser.strip() or saved.get("proxyUser", ""),
        password=payload.proxyPass or saved.get("proxyPassword", ""),
        scheme=payload.proxyScheme or "http",
    )


@app.post("/api/sub2api/groups")
async def sub2api_groups(payload: Sub2ApiGroupsPayload):
    """List SUB2API groups using the current target credentials (read-only)."""
    cfg = effective_config()
    base_url = payload.baseUrl.strip() or str(cfg.get("s2BaseUrl") or "").strip()
    admin_api_key = payload.adminApiKey.strip() or str(cfg.get("s2AdminApiKey") or "").strip()
    admin_email = payload.adminEmail.strip() or str(cfg.get("s2AdminEmail") or "").strip()
    admin_password = payload.adminPassword or str(cfg.get("s2AdminPassword") or "")
    group_name = str(cfg.get("s2GroupName") or "codex").strip() or "codex"
    if not base_url:
        return {"ok": False, "error": "请先填写并保存 SUB2API 服务地址"}
    if not admin_api_key and not (admin_email and admin_password):
        return {"ok": False, "error": "未配置管理员 API Key 或 JWT 兜底凭据，请先保存设置"}

    target = sub2api.Sub2ApiTarget(
        base_url=base_url,
        admin_api_key=admin_api_key,
        email=admin_email,
        password=admin_password,
        group_name=group_name,
    )
    try:
        headers = sub2api.auth_headers(target)
        groups = sub2api.list_groups(target, headers)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        for secret in (admin_api_key, admin_password, payload.adminApiKey.strip(), payload.adminPassword):
            if secret:
                message = message.replace(secret, "***")
        return {"ok": False, "error": message or "获取分组失败"}

    return {
        "ok": True,
        "groups": [
            {"id": group.get("id"), "name": str(group.get("name") or "")}
            for group in groups
            if isinstance(group, dict) and str(group.get("name") or "").strip()
        ],
    }


@app.post("/api/proxy/test")
async def proxy_test_route(payload: ProxyTestPayload):
    """Check which IP the proxy really exits from, and whether it looks residential."""
    proxy_url = _effective_proxy_url(payload)
    if not proxy_url:
        return JSONResponse(
            {"ok": False, "error": "没有可测试的代理：请填写代理主机/端口，或完整的代理 URL"},
            status_code=400,
        )
    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: proxy_test_module.test_proxy(proxy_url, proxy_insecure=payload.proxyInsecure),
    )
    return {"ok": True, **result}


@app.post("/api/defaults")
async def update_defaults(payload: LocalConfigPayload):
    values = payload.model_dump(exclude_unset=True)
    if not str(values.get("fiveSimApiKey") or "").strip():
        values.pop("fiveSimApiKey", None)
    saved = save_config(values)
    return {
        "ok": True,
        "envPath": str(local_config.ENV_PATH),
        "duckTokenPresent": bool(saved.get("duckToken")),
        "qqUserPresent": bool(saved.get("qqUser")),
        "qqPassPresent": bool(saved.get("qqPass")),
        "fiveSimApiKeyPresent": bool(saved.get("fiveSimApiKey")),
        "s2AdminApiKeySet": bool(saved.get("s2AdminApiKey")),
        "s2AdminPasswordSet": bool(saved.get("s2AdminPassword")),
    }


class QQImapTestPayload(BaseModel):
    user: str = ""
    password: str = ""
    host: str = "imap.qq.com"
    port: int = 993


@app.post("/api/qq/imap-test")
async def qq_imap_test(payload: QQImapTestPayload):
    cfg = effective_config()
    user = payload.user.strip() or cfg.get("qqUser", "").strip()
    password = payload.password or cfg.get("qqPass", "")
    if not user or not password:
        return JSONResponse({"ok": False, "error": "请填写 QQ 邮箱和 IMAP 授权码"}, status_code=400)
    config = qq_imap.QQImapConfig(
        user=user,
        password=password,
        host=payload.host.strip() or "imap.qq.com",
        port=max(1, min(65535, payload.port)),
    )
    try:
        await asyncio.get_event_loop().run_in_executor(None, qq_imap.check_qq_imap_login, config)
        return {"ok": True, "message": "QQ IMAP 登录成功"}
    except qq_imap.QQImapAuthError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ---------------- Accounts (MySQL canonical store) ----------------
@app.get("/api/accounts")
async def list_accounts():
    return [_account_summary(account) for account in AccountStore().list_accounts()]


@app.get("/api/accounts/{account_id}")
async def get_account_detail(account_id: int):
    account = AccountStore().get_account(int(account_id))
    if account is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    detail = dict(account)
    if detail.get("codexPhoneNumber"):
        detail["codexPhoneNumberMasked"] = f"••••{str(detail.pop('codexPhoneNumber'))[-4:]}"
    for key in _REDACTED_RESPONSE_KEYS:
        detail.pop(key, None)  # never expose stored credentials
    return detail


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int):
    if not AccountStore().delete_account(int(account_id)):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "id": int(account_id)}


@app.post("/api/accounts/cleanup")
async def cleanup_empty():
    store = AccountStore()
    removed: List[int] = []
    for account in store.list_accounts():
        session = account.get("session") if isinstance(account.get("session"), dict) else {}
        if session.get("accessToken"):
            continue
        if store.delete_account(int(account["id"])):
            removed.append(int(account["id"]))
    return {"removed": removed, "count": len(removed)}


class AccountValidityPayload(BaseModel):
    accountIds: List[int] = Field(default_factory=list)


@app.post("/api/accounts/validity-check")
async def check_account_validity(payload: AccountValidityPayload):
    """Local-only validity check: reads stored session fields and never contacts ChatGPT."""
    store = AccountStore()
    accounts = store.list_accounts()
    if payload.accountIds:
        wanted = {int(value) for value in payload.accountIds}
        accounts = [account for account in accounts if int(account["id"]) in wanted]

    results: List[dict] = []
    for account in accounts:
        validity = check_local_validity(account)
        stored = store.record_validity(int(account["id"]), validity)
        if stored is not None and isinstance(stored.get("localValidity"), dict):
            validity = stored["localValidity"]
        results.append({
            "id": int(account["id"]),
            "email": account.get("email") or "",
            "localValidity": validity,
        })
    return {
        "ok": True,
        "localOnly": True,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "results": results,
    }


class ReloginPayload(BaseModel):
    codeSource: str = "qq"
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None
    proxy: str = ""
    proxyScheme: str = ""
    proxyHost: str = ""
    proxyPort: str = ""
    proxyUser: str = ""
    proxyPass: str = ""
    proxyEnabled: bool = True
    proxyInsecure: bool = False
    headless: bool = False
    noPersistent: bool = False
    noClearTokens: bool = False
    stopOnError: bool = False
    cooldown: int = 10
    runCodexOauth: bool = False
    reloginLimit: int = 1
    reloginTimeout: int = 180


_AUTO_CODE_SOURCES = frozenset({"qq", "mhjc"})


def _build_relogin_args(p: ReloginPayload, *, account_id: int) -> List[str]:
    """Single-account re-login only; the batch "re-login every marked account" mode is retired."""
    args: List[str] = ["--mode", "relogin", "--relogin-id", str(int(account_id))]
    args += [
        "--code-source", p.codeSource,
        "--cooldown", str(max(0, p.cooldown)),
        "--relogin-limit", str(max(1, p.reloginLimit)),
        "--relogin-timeout", str(max(60, p.reloginTimeout)),
    ]
    if p.qqUser: args += ["--qq-user", p.qqUser]
    if p.qqPass: args += ["--qq-pass", p.qqPass]
    if p.qqMaxAttempts is not None: args += ["--qq-max-attempts", str(p.qqMaxAttempts)]
    if p.qqInterval is not None: args += ["--qq-interval", str(p.qqInterval)]
    if p.proxy: args += ["--proxy", p.proxy]
    if p.proxyScheme: args += ["--proxy-scheme", p.proxyScheme]
    if p.proxyHost: args += ["--proxy-host", p.proxyHost]
    if p.proxyPort: args += ["--proxy-port", p.proxyPort]
    if p.proxyUser: args += ["--proxy-user", p.proxyUser]
    if p.proxyPass: args += ["--proxy-pass", p.proxyPass]
    if not p.proxyEnabled: args.append("--no-proxy")
    if p.proxyInsecure: args.append("--proxy-insecure")
    if p.headless: args.append("--headless")
    if p.noPersistent: args.append("--no-persistent")
    if p.noClearTokens: args.append("--no-clear-tokens")
    if p.stopOnError: args.append("--stop-on-error")
    if p.runCodexOauth:
        args.append("--codex-oauth")
    else:
        args.append("--no-codex-oauth")
    return args


@app.post("/api/accounts/{account_id}/relogin")
async def relogin_account(account_id: int, payload: ReloginPayload):
    """Re-login one account by MySQL ID and refresh only that record's Cookie/session."""
    if payload.codeSource not in _AUTO_CODE_SOURCES:
        return JSONResponse({"ok": False, "error": "一键重登需要可自动收码的 code-source（qq 或 mhjc）"}, status_code=400)
    account = AccountStore().get_account(int(account_id))
    if account is None:
        return JSONResponse({"ok": False, "error": "账号不存在"}, status_code=404)
    args = _build_relogin_args(payload, account_id=int(account_id))
    ok, msg = await manager.start(args)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    return {
        "ok": True,
        "id": int(account_id),
        "email": account.get("email") or "",
        "args": _redact_args(args),
    }


# ---------------- Batch ----------------
class BatchPayload(BaseModel):
    emailSource: str = "duck-api"
    codeSource: str = "qq"
    authMode: str = "otp"
    count: int = 1
    cooldown: int = 30
    email: str = ""
    password: str = ""
    duckToken: str = ""
    duckBaseUrl: str = ""
    icloudHost: str = "auto"
    icloudFetchMode: str = "always-new"
    icloudLoginTimeout: Optional[int] = None
    mhjcApiKey: str = ""
    mhjcApiBase: str = ""
    mhjcUsername: str = ""
    mhjcTtl: Optional[int] = None
    mhjcMaxAttempts: Optional[int] = None
    mhjcInterval: Optional[float] = None
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None
    proxy: str = ""
    proxyScheme: str = ""
    proxyHost: str = ""
    proxyPort: str = ""
    proxyUser: str = ""
    proxyPass: str = ""
    proxyEnabled: bool = True
    proxyInsecure: bool = False
    headless: bool = False
    noPersistent: bool = False
    noClearTokens: bool = False
    stopOnError: bool = False
    runCodexOauth: bool = False
    noCodexOauth: Optional[bool] = None


def _build_args(p: BatchPayload) -> List[str]:
    args: List[str] = [
        "--email-source", p.emailSource,
        "--code-source", p.codeSource,
        "--auth-mode", p.authMode,
        "--count", str(max(1, p.count)),
        "--cooldown", str(max(0, p.cooldown)),
    ]
    if p.email: args += ["--email", p.email]
    if p.password: args += ["--password", p.password]
    if p.duckToken: args += ["--duck-token", p.duckToken]
    if p.duckBaseUrl: args += ["--duck-base-url", p.duckBaseUrl]
    if p.icloudHost: args += ["--icloud-host", p.icloudHost]
    if p.icloudFetchMode: args += ["--icloud-fetch-mode", p.icloudFetchMode]
    if p.icloudLoginTimeout is not None: args += ["--icloud-login-timeout", str(p.icloudLoginTimeout)]
    if p.mhjcApiKey: args += ["--mhjc-api-key", p.mhjcApiKey]
    if p.mhjcApiBase: args += ["--mhjc-api-base", p.mhjcApiBase]
    if p.mhjcUsername: args += ["--mhjc-username", p.mhjcUsername]
    if p.mhjcTtl is not None: args += ["--mhjc-ttl", str(p.mhjcTtl)]
    if p.mhjcMaxAttempts is not None: args += ["--mhjc-max-attempts", str(p.mhjcMaxAttempts)]
    if p.mhjcInterval is not None: args += ["--mhjc-interval", str(p.mhjcInterval)]
    if p.qqUser: args += ["--qq-user", p.qqUser]
    if p.qqPass: args += ["--qq-pass", p.qqPass]
    if p.qqMaxAttempts is not None: args += ["--qq-max-attempts", str(p.qqMaxAttempts)]
    if p.qqInterval is not None: args += ["--qq-interval", str(p.qqInterval)]
    if p.proxy: args += ["--proxy", p.proxy]
    if p.proxyScheme: args += ["--proxy-scheme", p.proxyScheme]
    if p.proxyHost: args += ["--proxy-host", p.proxyHost]
    if p.proxyPort: args += ["--proxy-port", p.proxyPort]
    if p.proxyUser: args += ["--proxy-user", p.proxyUser]
    if p.proxyPass: args += ["--proxy-pass", p.proxyPass]
    if not p.proxyEnabled: args.append("--no-proxy")
    if p.proxyInsecure: args.append("--proxy-insecure")
    if p.headless: args.append("--headless")
    if p.noPersistent: args.append("--no-persistent")
    if p.noClearTokens: args.append("--no-clear-tokens")
    if p.stopOnError: args.append("--stop-on-error")
    should_run_codex = bool(p.runCodexOauth)
    if p.noCodexOauth is not None:
        should_run_codex = not bool(p.noCodexOauth)
    if should_run_codex:
        args.append("--codex-oauth")
    else:
        args.append("--no-codex-oauth")
    return args


@app.post("/api/batch/start")
async def batch_start(payload: BatchPayload):
    args = _build_args(payload)
    ok, msg = await manager.start(args)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    return {"ok": True, "args": _redact_args(args)}


@app.post("/api/batch/stop")
async def batch_stop():
    stopped = await manager.stop()
    return {"ok": True, "stopped": stopped}


@app.get("/api/batch/status")
async def batch_status():
    return {"running": manager.is_running}


# ---------------- Codex 登录并推送 SUB2API ----------------
class CodexPushPayload(BaseModel):
    """Selected-account Codex login + SUB2API push.

    Target credentials come from `.env` unless the operator typed them into the
    dialog for this run only; no response echoes them back.
    """

    accountIds: List[int] = Field(default_factory=list)
    codeSource: str = "qq"
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None
    proxy: str = ""
    proxyScheme: str = ""
    proxyHost: str = ""
    proxyPort: str = ""
    proxyUser: str = ""
    proxyPass: str = ""
    proxyEnabled: bool = True
    proxyInsecure: bool = False
    headless: bool = False
    noPersistent: bool = False
    noClearTokens: bool = False
    cooldown: int = 5
    reloginTimeout: int = 180
    s2BaseUrl: str = ""
    s2AdminApiKey: str = ""
    s2AdminEmail: str = ""
    s2AdminPassword: str = ""
    s2GroupName: str = ""
    s2Concurrency: Optional[int] = None
    s2Priority: Optional[int] = None
    s2RateMultiplier: Optional[int] = None
    s2PrivacyMode: str = ""
    s2DryRun: bool = False
    saveTarget: bool = False


def _build_codex_push_args(payload: CodexPushPayload) -> List[str]:
    args: List[str] = [
        "--mode", "codex-push",
        "--code-source", payload.codeSource,
        "--cooldown", str(max(0, payload.cooldown)),
        "--relogin-timeout", str(max(60, payload.reloginTimeout)),
    ]
    for account_id in dict.fromkeys(int(value) for value in payload.accountIds):
        args += ["--push-id", str(account_id)]
    if payload.qqUser: args += ["--qq-user", payload.qqUser]
    if payload.qqPass: args += ["--qq-pass", payload.qqPass]
    if payload.qqMaxAttempts is not None: args += ["--qq-max-attempts", str(payload.qqMaxAttempts)]
    if payload.qqInterval is not None: args += ["--qq-interval", str(payload.qqInterval)]
    if payload.proxy: args += ["--proxy", payload.proxy]
    if payload.proxyScheme: args += ["--proxy-scheme", payload.proxyScheme]
    if payload.proxyHost: args += ["--proxy-host", payload.proxyHost]
    if payload.proxyPort: args += ["--proxy-port", payload.proxyPort]
    if payload.proxyUser: args += ["--proxy-user", payload.proxyUser]
    if payload.proxyPass: args += ["--proxy-pass", payload.proxyPass]
    if not payload.proxyEnabled: args.append("--no-proxy")
    if payload.proxyInsecure: args.append("--proxy-insecure")
    if payload.headless: args.append("--headless")
    if payload.noPersistent: args.append("--no-persistent")
    if payload.noClearTokens: args.append("--no-clear-tokens")
    if payload.s2BaseUrl: args += ["--s2-base-url", payload.s2BaseUrl]
    # When the dialog asked us to persist the target, `.env` already holds the
    # secrets and the child reads them from there — keeping them out of `ps`.
    if payload.s2AdminApiKey and not payload.saveTarget:
        args += ["--s2-admin-api-key", payload.s2AdminApiKey]
    if payload.s2AdminEmail: args += ["--s2-admin-email", payload.s2AdminEmail]
    if payload.s2AdminPassword and not payload.saveTarget:
        args += ["--s2-admin-password", payload.s2AdminPassword]
    if payload.s2GroupName: args += ["--s2-group-name", payload.s2GroupName]
    if payload.s2Concurrency is not None: args += ["--s2-concurrency", str(payload.s2Concurrency)]
    if payload.s2Priority is not None: args += ["--s2-priority", str(payload.s2Priority)]
    if payload.s2RateMultiplier is not None: args += ["--s2-rate-multiplier", str(payload.s2RateMultiplier)]
    if payload.s2PrivacyMode: args += ["--s2-privacy-mode", payload.s2PrivacyMode]
    if payload.s2DryRun: args.append("--s2-dry-run")
    return args


_CODEX_PUSH_RESULT_PREFIX = "[codex-push-result] "


def _codex_push_results() -> List[dict]:
    """Per-account outcomes parsed from the running task's structured log lines."""
    results: "dict[int, dict]" = {}
    for line in manager.recent:
        if not line.startswith(_CODEX_PUSH_RESULT_PREFIX):
            continue
        try:
            parsed = json.loads(line[len(_CODEX_PUSH_RESULT_PREFIX):])
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict) or "id" not in parsed:
            continue
        results[int(parsed["id"])] = parsed  # a repeated account keeps its latest outcome
    return list(results.values())


@app.post("/api/accounts/codex-push")
async def codex_push(payload: CodexPushPayload):
    """Click-to-run: fresh Codex login for the selected accounts, then push each one."""
    if payload.codeSource not in _AUTO_CODE_SOURCES:
        return JSONResponse(
            {"ok": False, "error": "自动登录需要可自动收码的 code-source（qq 或 mhjc），手动收码无法在后台完成"},
            status_code=400,
        )
    if not payload.accountIds:
        return JSONResponse({"ok": False, "error": "请先勾选要处理的账号"}, status_code=400)

    store = AccountStore()
    selected: List[dict] = []
    for account_id in dict.fromkeys(int(value) for value in payload.accountIds):
        account = store.get_account(account_id)
        if account is None:
            return JSONResponse({"ok": False, "error": f"账号 ID={account_id} 不存在"}, status_code=404)
        selected.append(account)

    saved = effective_config()
    base_url = (payload.s2BaseUrl or saved.get("s2BaseUrl") or "").strip()
    api_key = (payload.s2AdminApiKey or saved.get("s2AdminApiKey") or "").strip()
    admin_email = (payload.s2AdminEmail or saved.get("s2AdminEmail") or "").strip()
    admin_password = payload.s2AdminPassword or saved.get("s2AdminPassword") or ""
    if not base_url:
        return JSONResponse({"ok": False, "error": "请先填写 SUB2API base URL"}, status_code=400)
    if not api_key and not (admin_email and admin_password):
        return JSONResponse(
            {"ok": False, "error": "请先填写 SUB2API 管理员 API Key（x-api-key）；"
                                   "该部署若未启用 API Key，可同时填写管理员邮箱和密码走 JWT 兜底"},
            status_code=400,
        )

    if payload.saveTarget:
        save_config({
            "s2BaseUrl": base_url,
            "s2AdminApiKey": api_key,
            "s2AdminEmail": admin_email,
            "s2AdminPassword": admin_password,
            "s2GroupName": payload.s2GroupName or saved.get("s2GroupName") or "codex",
            "s2Concurrency": payload.s2Concurrency if payload.s2Concurrency is not None else saved.get("s2Concurrency"),
            "s2Priority": payload.s2Priority if payload.s2Priority is not None else saved.get("s2Priority"),
            "s2RateMultiplier": payload.s2RateMultiplier if payload.s2RateMultiplier is not None else saved.get("s2RateMultiplier"),
            "s2PrivacyMode": payload.s2PrivacyMode or saved.get("s2PrivacyMode") or "training_off",
        })

    args = _build_codex_push_args(payload)
    ok, msg = await manager.start(args)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    return {
        "ok": True,
        "count": len(selected),
        "emails": [account.get("email") or "" for account in selected],
        "args": _redact_args(args),
    }


@app.get("/api/accounts/codex-push/status")
async def codex_push_status():
    """Truthful per-account progress for the selected-account login+push task."""
    results = _codex_push_results()
    return {
        "running": manager.is_running,
        "selected": len(results),
        "done": sum(1 for row in results if row.get("pushed") or row.get("error")),
        "pushed": sum(1 for row in results if row.get("pushed")),
        "failed": sum(1 for row in results if row.get("error")),
        "results": results,
    }


@app.get("/api/batch/stream")
async def batch_stream():
    q = manager.subscribe()

    async def gen():
        try:
            # 初始化心跳，保证连接立刻 flush
            yield "data: \n\n"
            while True:
                msg = await q.get()
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
        finally:
            manager.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------- 封禁检测（ban-scan） ----------------
class BanScanPayload(BaseModel):
    """Scan the account mailboxes for OpenAI deactivation notices."""

    accountIds: List[int] = Field(default_factory=list)
    sinceDays: int = 30
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None


def _build_ban_scan_args(payload: BanScanPayload) -> List[str]:
    args: List[str] = [
        "--mode", "ban-scan",
        "--ban-since-days", str(max(1, payload.sinceDays)),
    ]
    for account_id in dict.fromkeys(int(value) for value in payload.accountIds):
        args += ["--ban-id", str(account_id)]
    if payload.qqUser: args += ["--qq-user", payload.qqUser]
    if payload.qqPass: args += ["--qq-pass", payload.qqPass]
    if payload.qqMaxAttempts is not None: args += ["--qq-max-attempts", str(payload.qqMaxAttempts)]
    if payload.qqInterval is not None: args += ["--qq-interval", str(payload.qqInterval)]
    return args


_BAN_SCAN_RESULT_PREFIX = "[ban-scan-result] "


def _ban_scan_results() -> List[dict]:
    results: "dict[int, dict]" = {}
    for line in manager.recent:
        if not line.startswith(_BAN_SCAN_RESULT_PREFIX):
            continue
        try:
            parsed = json.loads(line[len(_BAN_SCAN_RESULT_PREFIX):])
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict) or "id" not in parsed:
            continue
        results[int(parsed["id"])] = parsed
    return list(results.values())


@app.post("/api/accounts/ban-scan")
async def ban_scan(payload: BanScanPayload):
    """Start the mailbox scan that marks banned accounts in MySQL."""
    store = AccountStore()
    accounts = store.list_accounts()
    if not accounts:
        return JSONResponse({"ok": False, "error": "MySQL 中还没有账号"}, status_code=400)

    selected = accounts
    if payload.accountIds:
        wanted = list(dict.fromkeys(int(value) for value in payload.accountIds))
        by_id = {int(account["id"]): account for account in accounts}
        missing = [account_id for account_id in wanted if account_id not in by_id]
        if missing:
            return JSONResponse({"ok": False, "error": f"账号 ID={missing[0]} 不存在"}, status_code=404)
        selected = [by_id[account_id] for account_id in wanted]

    args = _build_ban_scan_args(payload)
    ok, msg = await manager.start(args)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    return {
        "ok": True,
        "count": len(selected),
        "emails": [account.get("email") or "" for account in selected],
        "args": _redact_args(args),
    }


@app.get("/api/accounts/ban-scan/status")
async def ban_scan_status():
    results = _ban_scan_results()
    return {
        "running": manager.is_running,
        "marked": sum(1 for row in results if row.get("banned")),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 5sim 接码平台
# ---------------------------------------------------------------------------
class AcCheckPayload(BaseModel):
    token: str = ""
    accountId: int = 0
    baseUrl: str = ""
    promoId: str = ""


class AcBatchCheckPayload(BaseModel):
    tokens: list[str] = []
    accountIds: list[int] = []
    allAccounts: bool = False
    baseUrl: str = ""
    promoId: str = ""


def _ac_effective_options(base_url: str = "", promo_id: str = "") -> dict[str, str]:
    cfg = effective_config()
    return {
        "base_url": base_url.strip() or cfg.get("acCheckerBaseUrl") or ac_checker.DEFAULT_BASE_URL,
        "promo_id": promo_id.strip() or cfg.get("acCheckerPromoId") or ac_checker.DEFAULT_PROMO_ID,
        "proxy": cfg.get("proxy") or "",
        "proxy_insecure": _cfg_proxy_insecure(cfg),
    }


def _load_account_record(store: AccountStore, account_id: int) -> dict[str, Any]:
    account = store.get_account(int(account_id), include_password=True)
    if account is None:
        raise FileNotFoundError(f"账号 ID={account_id} 不存在")
    return account


def _save_ac_check(store: AccountStore, account: dict[str, Any], result: dict[str, Any]) -> Optional[int]:
    """Persist an AC result onto the same MySQL record; returns the record ID."""
    payload = _account_record_payload(account)
    payload["acCheck"] = result
    saved = store.save_account(
        str(account.get("email") or ""), str(account.get("password") or ""), payload,
    )
    return int(saved["id"]) if saved else None


def _run_ac_single(payload: AcCheckPayload) -> dict[str, Any]:
    options = _ac_effective_options(payload.baseUrl, payload.promoId)
    store = AccountStore()
    account: Optional[dict[str, Any]] = None
    account_email = ""
    token_source = "manual"
    token = payload.token.strip()
    if payload.accountId:
        account = _load_account_record(store, payload.accountId)
        token_info = ac_checker.extract_account_token(account)
        token = token_info["token"]
        token_source = token_info.get("tokenSource") or "account"
        account_email = token_info.get("accountEmail") or account.get("email") or ""
    elif not token:
        raise ValueError("请提供 token 或 MySQL 账号 ID")

    result = ac_checker.check_token(
        token,
        base_url=options["base_url"],
        promo_id=options["promo_id"],
        proxy=options["proxy"] or None,
        proxy_insecure=bool(options["proxy_insecure"]),
    )
    normalized = {
        **result,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "baseUrl": ac_checker.normalize_base_url(options["base_url"]),
        "promoId": ac_checker.resolve_promo_id(options["promo_id"]),
        "tokenSource": token_source,
        "accountEmail": account_email or result.get("email") or "",
    }
    if account is not None:
        saved_id = _save_ac_check(store, account, normalized)
        if saved_id is not None:
            normalized["accountId"] = saved_id
    return normalized


def _run_ac_batch(payload: AcBatchCheckPayload) -> dict[str, Any]:
    options = _ac_effective_options(payload.baseUrl, payload.promoId)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    store = AccountStore()
    for token in payload.tokens:
        raw = str(token or "").strip()
        if raw:
            rows.append({"token": raw, "tokenSource": "manual", "accountId": None, "accountEmail": "", "account": None})

    account_ids = [int(value) for value in payload.accountIds]
    if payload.allAccounts:
        account_ids.extend(int(account["id"]) for account in store.list_accounts())

    seen_ids: set[int] = set()
    for account_id in account_ids:
        if account_id in seen_ids:
            continue
        seen_ids.add(account_id)
        try:
            account = _load_account_record(store, account_id)
            token_info = ac_checker.extract_account_token(account)
        except Exception as e:  # noqa: BLE001
            skipped.append({"accountId": account_id, "reason": str(e)})
            continue
        rows.append({
            "token": token_info["token"],
            "tokenSource": token_info.get("tokenSource") or "account",
            "accountId": account_id,
            "accountEmail": token_info.get("accountEmail") or account.get("email") or "",
            "account": account,
        })

    if not rows:
        return {
            "count": 0,
            "promo_id": ac_checker.resolve_promo_id(options["promo_id"]),
            "results": [],
            "skipped": skipped,
        }

    batch = ac_checker.check_tokens(
        [row["token"] for row in rows],
        base_url=options["base_url"],
        promo_id=options["promo_id"],
        proxy=options["proxy"] or None,
        proxy_insecure=bool(options["proxy_insecure"]),
    )
    results: list[dict[str, Any]] = []
    checked_at = datetime.now(timezone.utc).isoformat()
    for idx, row in enumerate(rows):
        item = dict(batch["results"][idx]) if idx < len(batch["results"]) else {}
        normalized = {
            **item,
            "checkedAt": checked_at,
            "baseUrl": ac_checker.normalize_base_url(options["base_url"]),
            "promoId": batch.get("promo_id") or ac_checker.resolve_promo_id(options["promo_id"]),
            "tokenSource": row["tokenSource"],
            "accountEmail": row["accountEmail"] or item.get("email") or "",
            "accountId": row["accountId"],
        }
        if row["account"] is not None:
            saved_id = _save_ac_check(store, row["account"], normalized)
            if saved_id is not None:
                normalized["accountId"] = saved_id
        results.append(normalized)

    return {
        "count": len(results),
        "promo_id": batch.get("promo_id") or ac_checker.resolve_promo_id(options["promo_id"]),
        "results": results,
        "skipped": skipped,
    }


@app.get("/api/ac/health")
async def ac_health(baseUrl: str = ""):
    options = _ac_effective_options(baseUrl, "")
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ac_checker.healthz(
                base_url=options["base_url"],
                proxy=options["proxy"] or None,
                proxy_insecure=bool(options["proxy_insecure"]),
            ),
        )
        return {"ok": True, "health": data, "baseUrl": ac_checker.normalize_base_url(options["base_url"])}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/ac/check")
async def ac_check(payload: AcCheckPayload):
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, lambda: _run_ac_single(payload))
        return {"ok": True, "result": result}
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "MySQL 中不存在该账号 ID"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/ac/check-batch")
async def ac_check_batch(payload: AcBatchCheckPayload):
    try:
        result = await asyncio.get_event_loop().run_in_executor(None, lambda: _run_ac_batch(payload))
        return {"ok": True, **result}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# PAY.153 checkout task bridge
# ---------------------------------------------------------------------------
class Pay153CheckoutPayload(BaseModel):
    accountId: int
    plan: str = "plus"
    linkType: str = "hosted"
    country: str = "US"
    currency: str = "USD"
    entryProxies: list[str] = Field(default_factory=list)
    exitProxies: list[str] = Field(default_factory=list)
    retryCount: int = 10
    useSentinel: bool = True
    usePromo: bool = True
    promoCampaign: str = ""
    promoCode: str = ""
    workspaceName: str = "Codex Workspace"
    workspaceId: str = ""
    seatQuantity: int = 5
    priceInterval: str = "month"
    creditQuantity: int = 13
    pixTaxId: str = ""
    pixAutoKind: str = "cpf"
    acceptedThirdParty: bool = False


class Pay153CancelPayload(BaseModel):
    jobId: str


_pay153_jobs: dict[str, dict[str, Any]] = {}


def _pay153_connection_options() -> dict[str, Any]:
    cfg = effective_config()
    return {
        "base_url": pay153.normalize_base_url(cfg.get("pay153BaseUrl") or pay153.DEFAULT_BASE_URL),
        "proxy": cfg.get("proxy") or None,
        "proxy_insecure": _cfg_proxy_insecure(cfg),
    }


def _pay153_request_options(payload: Pay153CheckoutPayload) -> dict[str, Any]:
    return {
        "plan": payload.plan,
        "link_type": payload.linkType,
        "country": payload.country,
        "currency": payload.currency,
        "entry_proxies": payload.entryProxies,
        "exit_proxies": payload.exitProxies,
        "retry_count": payload.retryCount,
        "use_sen": payload.useSentinel,
        "use_so": payload.useSentinel,
        "use_promo": payload.usePromo,
        "promo_campaign": payload.promoCampaign,
        "promo_code": payload.promoCode,
        "workspace_name": payload.workspaceName,
        "workspace_id": payload.workspaceId,
        "seat_quantity": payload.seatQuantity,
        "price_interval": payload.priceInterval,
        "credit_quantity": payload.creditQuantity,
        "pix_tax_id": payload.pixTaxId,
        "pix_auto_kind": payload.pixAutoKind,
    }


def _remember_pay153_job(job_id: str, context: dict[str, Any]) -> None:
    _pay153_jobs[job_id] = context
    while len(_pay153_jobs) > 200:
        _pay153_jobs.pop(next(iter(_pay153_jobs)))


def _save_pay153_result(context: dict[str, Any], progress: dict[str, Any]) -> None:
    store: Optional[AccountStore] = context.get("store")
    account: Optional[dict[str, Any]] = context.get("account")
    if store is None or account is None:
        return
    status = str(progress.get("status") or "")
    snapshot = {
        "status": status,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "baseUrl": context["base_url"],
        "jobId": context["job_id"],
        "plan": context["plan"],
        "linkType": context["link_type"],
        "accountEmail": context.get("account_email") or "",
    }
    if isinstance(progress.get("result"), dict):
        snapshot["result"] = progress["result"]
    if progress.get("error"):
        snapshot["error"] = str(progress["error"])
    payload = _account_record_payload(account)
    payload["payurlCheck"] = snapshot
    store.save_account(str(account.get("email") or ""), str(account.get("password") or ""), payload)


@app.post("/api/pay153/checkout")
async def pay153_checkout(payload: Pay153CheckoutPayload):
    if not payload.acceptedThirdParty:
        return JSONResponse(
            {"ok": False, "error": "请先确认账号 access token 将发送到第三方 PAY.153 服务"},
            status_code=400,
        )
    try:
        store = AccountStore()
        account = _load_account_record(store, payload.accountId)
        token_info = ac_checker.extract_account_token(account)
        request_options = _pay153_request_options(payload)
        connection = _pay153_connection_options()
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: pay153.create_checkout(
                token_info["token"],
                request_options,
                base_url=connection["base_url"],
                proxy=connection["proxy"],
                proxy_insecure=connection["proxy_insecure"],
            ),
        )
        job_id = str(result["job_id"])
        normalized_options = pay153.build_checkout_payload(token_info["token"], request_options)
        _remember_pay153_job(job_id, {
            "job_id": job_id,
            "store": store,
            "account": account,
            "account_id": int(account["id"]),
            "base_url": connection["base_url"],
            "proxy": connection["proxy"],
            "proxy_insecure": connection["proxy_insecure"],
            "account_email": token_info.get("accountEmail") or account.get("email") or "",
            "plan": normalized_options["plan"],
            "link_type": normalized_options["link_type"],
        })
        return {
            "ok": True,
            "jobId": job_id,
            "queuePosition": int(result.get("queue_position") or 0),
            "internal": bool(result.get("internal")),
        }
    except FileNotFoundError:
        return JSONResponse({"ok": False, "error": "MySQL 中不存在该账号 ID"}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.get("/api/pay153/progress")
async def pay153_progress(job_id: str):
    context = _pay153_jobs.get(job_id)
    if context is None:
        return JSONResponse({"ok": False, "error": "任务不存在或服务已重启"}, status_code=404)
    try:
        progress = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: pay153.get_progress(
                job_id,
                base_url=context["base_url"],
                proxy=context["proxy"],
                proxy_insecure=context["proxy_insecure"],
            ),
        )
        status = str(progress.get("status") or "")
        if status in pay153.TERMINAL_STATUSES and not context.get("saved"):
            _save_pay153_result(context, progress)
            context["saved"] = True
        return {"ok": True, **progress}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.post("/api/pay153/cancel")
async def pay153_cancel(payload: Pay153CancelPayload):
    context = _pay153_jobs.get(payload.jobId)
    if context is None:
        return JSONResponse({"ok": False, "error": "任务不存在或服务已重启"}, status_code=404)
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: pay153.cancel_checkout(
                payload.jobId,
                base_url=context["base_url"],
                proxy=context["proxy"],
                proxy_insecure=context["proxy_insecure"],
            ),
        )
        return {"ok": True, "result": result}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


class FiveSimPricesPayload(BaseModel):
    product: str = "openai"
    country: str = ""


class FiveSimBuyPayload(BaseModel):
    apiKey: str = ""
    country: str
    operator: str = "any"
    product: str = "openai"
    maxPrice: Optional[float] = None


class FiveSimApiKeyPayload(BaseModel):
    apiKey: str = ""


def _configured_fivesim_api_key(supplied: str = "") -> str:
    """Prefer a request key, otherwise use the server-side saved secret."""
    return (supplied or "").strip() or str(effective_config().get("fiveSimApiKey") or "").strip()


class FiveSimReusePayload(BaseModel):
    apiKey: str = ""
    phone: str = ""
    country: str = "any"
    operator: str = "any"
    product: str = "openai"


class FiveSimPoolRemovePayload(BaseModel):
    phone: str
    product: str = "openai"


@app.post("/api/5sim/prices")
async def five_sim_prices(payload: FiveSimPricesPayload):
    """查询 5sim 价格，按接码率降序排列。"""
    cfg = effective_config()
    try:
        entries = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.query_prices(
                product=payload.product,
                country=payload.country,
                proxy=cfg.get("proxy") or None,
                proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"},
            ),
        )
        result = [
            {
                "country": e.country,
                "operator": e.operator,
                "product": e.product,
                "cost": e.cost,
                "count": e.count,
                "rate": e.rate,
                "rateStr": e.rate_str,
            }
            for e in entries
        ]
        return {
            "ok": True,
            "total": len(result),
            "entries": result,
            "countries": sorted(set(e["country"] for e in result)),
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/5sim/buy")
async def five_sim_buy(payload: FiveSimBuyPayload):
    """购买激活号码。"""
    api_key = _configured_fivesim_api_key(payload.apiKey)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先在短信设置中配置 5sim API key"}, status_code=400)
    cfg = effective_config()
    try:
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.buy_activation(
                api_key=api_key,
                country=payload.country,
                operator=payload.operator,
                product=payload.product,
                max_price=payload.maxPrice,
                proxy=cfg.get("proxy") or None,
                proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"},
            ),
        )
        return {
            "ok": True,
            "order": {
                "id": order.id,
                "phone": order.phone,
                "operator": order.operator,
                "product": order.product,
                "price": order.price,
                "status": order.status,
                "expires": order.expires,
                "sms": order.sms,
                "country": order.country,
                "code": order.code,
            },
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/5sim/check/{order_id}")
async def five_sim_check(order_id: int, payload: FiveSimApiKeyPayload):
    """检查订单状态 / 获取短信。"""
    api_key = _configured_fivesim_api_key(payload.apiKey)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先在短信设置中配置 5sim API key"}, status_code=400)
    cfg = effective_config()
    try:
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.check_order(api_key=api_key, order_id=order_id,
                                        proxy=cfg.get("proxy") or None,
                                        proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"}),
        )
        return {
            "ok": True,
            "order": {
                "id": order.id,
                "phone": order.phone,
                "status": order.status,
                "sms": order.sms,
                "code": order.code,
                "raw": order.raw,
            },
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/5sim/cancel/{order_id}")
async def five_sim_cancel(order_id: int, payload: FiveSimApiKeyPayload):
    """取消订单。"""
    api_key = _configured_fivesim_api_key(payload.apiKey)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先在短信设置中配置 5sim API key"}, status_code=400)
    cfg = effective_config()
    try:
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.cancel_order(api_key=api_key, order_id=order_id,
                                         proxy=cfg.get("proxy") or None,
                                         proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"}),
        )
        return {"ok": True, "order": {"id": order.id, "status": order.status}}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/5sim/finish/{order_id}")
async def five_sim_finish(order_id: int, payload: FiveSimApiKeyPayload):
    """确认完成订单（收到短信后）。"""
    api_key = _configured_fivesim_api_key(payload.apiKey)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先在短信设置中配置 5sim API key"}, status_code=400)
    cfg = effective_config()
    try:
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.finish_order(api_key=api_key, order_id=order_id,
                                         proxy=cfg.get("proxy") or None,
                                         proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"}),
        )
        return {"ok": True, "order": {"id": order.id, "status": order.status}}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/5sim/profile")
async def five_sim_profile(payload: FiveSimApiKeyPayload):
    """获取 5sim 账户信息。"""
    api_key = _configured_fivesim_api_key(payload.apiKey)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先在短信设置中配置 5sim API key"}, status_code=400)
    cfg = effective_config()
    try:
        profile = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.get_profile(api_key=api_key,
                                        proxy=cfg.get("proxy") or None,
                                        proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"}),
        )
        return {"ok": True, "profile": profile}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# — 复用池管理 —


@app.get("/api/5sim/pool")
async def five_sim_pool():
    """获取当前复用池。"""
    pool = num5sim.ActivationPool.load()
    pool.prune_expired()
    entries = pool.to_list()
    return {"ok": True, "pool": entries, "total": len(entries)}


@app.post("/api/5sim/reuse")
async def five_sim_reuse(payload: FiveSimReusePayload):
    """手动复用池中号码（从池中找到可复用号码并调用 5sim reuse API）。"""
    api_key = _configured_fivesim_api_key(payload.apiKey)
    if not api_key:
        return JSONResponse({"ok": False, "error": "请先在短信设置中配置 5sim API key"}, status_code=400)
    cfg = effective_config()
    pool = num5sim.ActivationPool.load()
    pool.prune_expired()
    try:
        if payload.phone:
            reusable = next((entry for entry in pool.entries
                             if entry.phone == payload.phone and entry.product == payload.product), None)
        else:
            reusable = pool.find_usable(
                country=payload.country if payload.country != "any" else "",
                operator=payload.operator if payload.operator != "any" else "",
                product=payload.product,
            )
        if not reusable:
            return JSONResponse({"ok": False, "error": "复用池中没有可用号码"}, status_code=404)
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: num5sim.reuse_number(
                api_key=api_key,
                phone=reusable.phone,
                product=payload.product,
                proxy=cfg.get("proxy") or None,
                proxy_insecure=str(cfg.get("proxyInsecure") or "").strip().lower() in {"1", "true", "yes", "on"},
            ),
        )
        # 复用成功后增加计数
        pool.add_or_update(
            phone=order.phone, country=order.country or payload.country,
            operator=order.operator or payload.operator,
            product=payload.product, order_id=order.id,
        )
        pool.prune_expired()
        pool.save()
        return {
            "ok": True,
            "order": {
                "id": order.id, "phone": order.phone, "status": order.status,
                "country": order.country, "operator": order.operator, "price": order.price,
            },
            "reused_from_pool": True,
        }
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/5sim/pool/remove")
async def five_sim_pool_remove(payload: FiveSimPoolRemovePayload):
    """从复用池中移除指定号码。"""
    pool = num5sim.ActivationPool.load()
    pool.remove(phone=payload.phone, product=payload.product)
    pool.save()
    return {"ok": True}


@app.post("/api/5sim/pool/clear")
async def five_sim_pool_clear():
    """清空复用池。"""
    pool = num5sim.ActivationPool()
    pool.save()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    import uvicorn
    host = os.environ.get("QR_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("QR_WEB_PORT", "8765"))
    print(f"[webui] starting on http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
