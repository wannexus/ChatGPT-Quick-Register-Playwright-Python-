"""把账号 JSON 转 / 推到 SUB2API 的工具。

SUB2API 管理端鉴权（参考上游 Wei-Shaw/sub2api 的 sub2api-admin skill）：
  首选  x-api-key: <管理员 API Key>
  兜底  Authorization: Bearer <管理员 JWT>（POST /api/v1/auth/login 取得）

用到的端点：
  POST /api/v1/auth/login                {email, password} -> {access_token, ...}（仅 JWT 兜底）
  GET  /api/v1/admin/groups/all          分组列表
  POST /api/v1/admin/accounts            创建账号

所有响应包了一层 {code: 0, message: "...", data: {...}}；code != 0 视为失败。
鉴权失败返回 INVALID_ADMIN_KEY，此时需在后台重新生成管理员 API Key。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from core.http_utils import open_url

# 用浏览器化的 UA + headers，避免 Cloudflare 的 1010 / 1020 拦截
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


SUB2API_DEFAULTS = {
    "concurrency": 5,
    "priority": 1,
    "rate_multiplier": 1,
    "group_name": "codex",
    "auto_pause_on_expired": True,
    "privacy_mode": "training_off",
}

# SUB2API export 时 credentials 期望的所有字段（按用户提供的样例）
CRED_KEYS_FROM_CODEX = (
    "access_token", "chatgpt_account_id", "chatgpt_user_id",
    "client_id", "email", "expires_at", "id_token",
    "model_mapping", "organization_id", "plan_type", "refresh_token",
)


@dataclass
class Sub2ApiTarget:
    base_url: str
    email: str = ""            # JWT fallback only (POST /api/v1/auth/login)
    password: str = ""         # JWT fallback only
    admin_api_key: str = ""    # preferred auth: x-api-key header
    group_name: str = "codex"
    concurrency: int = 5
    priority: int = 1
    rate_multiplier: int = 1
    auto_pause_on_expired: bool = True
    privacy_mode: str = "training_off"
    proxy: Optional[str] = None  # 走代理访问 SUB2API 后端（可选）
    proxy_insecure: bool = False

    def origin(self) -> str:
        """只保留 scheme://host[:port]，丢掉 path/query/fragment。
        用户即便贴了 `https://x.com/admin/groups` 也能被规整成 `https://x.com`。"""
        raw = self.base_url.strip()
        if not raw:
            raise ValueError("base_url 为空")
        if not raw.startswith(("http://", "https://")):
            raw = "https://" + raw  # 默认 https
        parsed = urllib.parse.urlparse(raw)
        if not parsed.netloc:
            raise ValueError(f"无法解析 base_url：{self.base_url!r}")
        return f"{parsed.scheme}://{parsed.netloc}"


# ---------------------------------------------------------------------------
# JSON -> SUB2API payload
# ---------------------------------------------------------------------------

def _credentials_from_codex_auth(codex: Dict[str, Any], *, fallback_email: str) -> Dict[str, Any]:
    """从 register.py 注入的 codexAuth 字段拼 credentials（最完整）。"""
    out: Dict[str, Any] = {}
    for key in CRED_KEYS_FROM_CODEX:
        if key == "model_mapping":
            mm = codex.get("model_mapping")
            if isinstance(mm, dict) and mm:
                out["model_mapping"] = dict(mm)
            continue
        v = codex.get(key)
        if v is not None and v != "":
            out[key] = v
    if "email" not in out and fallback_email:
        out["email"] = fallback_email
    if not out.get("access_token"):
        raise ValueError("codexAuth 缺 access_token")
    return out


def _credentials_from_session(session: Dict[str, Any], *, fallback_email: str) -> Dict[str, Any]:
    """老路径：只用 web session.accessToken（**无 refresh_token**）。SUB2API
    虽然能存，但过期就废，建议升级跑 codex OAuth。"""
    access_token = session.get("accessToken") or ""
    if not access_token:
        raise ValueError("session 也没 accessToken")
    user = session.get("user") if isinstance(session, dict) else {}
    user = user if isinstance(user, dict) else {}
    creds: Dict[str, Any] = {
        "access_token": access_token,
        "email": user.get("email") or fallback_email,
    }
    if session.get("expires"):
        creds["expires_at"] = session["expires"]
    if user.get("id"):
        creds["chatgpt_user_id"] = str(user["id"])
    return creds


def build_payload_from_account(
    account: Dict[str, Any],
    *,
    concurrency: int = 5,
    priority: int = 1,
    rate_multiplier: int = 1,
    auto_pause_on_expired: bool = True,
    privacy_mode: str = "training_off",
    group_ids: Optional[List[int]] = None,
    notes: str = "",
) -> Dict[str, Any]:
    """从单个账号 JSON 构造 SUB2API 账号 payload。

    优先级：codexAuth > session.accessToken。
    输出形如用户给的样例：{name, platform, type, credentials, extra, concurrency, ...}
    """
    fallback_email = (account.get("email") or "").strip()
    codex = account.get("codexAuth")
    if isinstance(codex, dict) and codex.get("access_token"):
        credentials = _credentials_from_codex_auth(codex, fallback_email=fallback_email)
    else:
        session = account.get("session") if isinstance(account.get("session"), dict) else {}
        credentials = _credentials_from_session(session or {}, fallback_email=fallback_email)

    name = credentials.get("email") or fallback_email or "imported"

    payload: Dict[str, Any] = {
        "name": name,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {"privacy_mode": privacy_mode},
        "concurrency": int(concurrency),
        "priority": int(priority),
        "rate_multiplier": int(rate_multiplier),
        "auto_pause_on_expired": bool(auto_pause_on_expired),
    }
    if notes:
        payload["notes"] = notes
    if group_ids:
        payload["group_ids"] = [int(g) for g in group_ids]
    return payload


def collect_accounts_from_dir(
    accounts: Iterable[Mapping[str, Any]],
    *,
    skip_empty: bool = True,
    require_codex: bool = False,
) -> List[Tuple[str, Dict[str, Any]]]:
    """筛选 MySQL 账号记录（label 为 email）。
    skip_empty=True 时跳过既没 codexAuth.access_token 也没 session.accessToken 的；
    require_codex=True 时只保留有完整 codexAuth 的（推荐用于 SUB2API 导出）。
    """
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for account in accounts:
        data = dict(account or {})
        codex = data.get("codexAuth")
        has_codex = isinstance(codex, dict) and bool(codex.get("access_token"))
        sess = data.get("session") if isinstance(data.get("session"), dict) else None
        has_session = bool(sess and sess.get("accessToken"))

        if require_codex and not has_codex:
            continue
        if skip_empty and not (has_codex or has_session):
            continue
        label = str(data.get("email") or "").strip() or f"id={data.get('id')}"
        rows.append((label, data))
    return rows


def build_export_bundle(
    accounts: Iterable[Mapping[str, Any]],
    *,
    concurrency: int = 5,
    priority: int = 1,
    rate_multiplier: int = 1,
    auto_pause_on_expired: bool = True,
    privacy_mode: str = "training_off",
    skip_empty: bool = True,
    require_codex: bool = False,
    group_ids: Optional[List[int]] = None,
    proxies: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """生成 sub2api 批量导入 JSON，与用户给的样例同构：
    { exported_at, proxies, accounts: [...] }
    """
    accepted: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for label, account in collect_accounts_from_dir(accounts, skip_empty=skip_empty, require_codex=require_codex):
        try:
            payload = build_payload_from_account(
                account,
                concurrency=concurrency,
                priority=priority,
                rate_multiplier=rate_multiplier,
                auto_pause_on_expired=auto_pause_on_expired,
                privacy_mode=privacy_mode,
                group_ids=group_ids,
            )
            accepted.append(payload)
        except Exception as e:  # noqa: BLE001
            skipped.append({"email": label, "reason": str(e)})

    from datetime import datetime, timezone
    bundle: Dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "proxies": list(proxies or []),
        "accounts": accepted,
    }
    # 不写到导出 JSON 里，但作为元数据返回给调用者
    bundle["_meta"] = {
        "count": len(accepted),
        "skipped": skipped,
    }
    return bundle


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _request_json(
    origin: str,
    path: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    body: Any = None,
    proxy: Optional[str] = None,
    proxy_insecure: bool = False,
    timeout: float = 20.0,
) -> Any:
    url = f"{origin.rstrip('/')}{path}"
    parsed_origin = urllib.parse.urlparse(origin)
    referer_root = f"{parsed_origin.scheme}://{parsed_origin.netloc}/admin"
    request_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": _BROWSER_UA,
        "Origin": f"{parsed_origin.scheme}://{parsed_origin.netloc}",
        "Referer": referer_root,
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    request_headers.update(dict(headers or {}))
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, method=method.upper(), data=data, headers=request_headers)
    try:
        with open_url(req, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        text = raw.decode("utf-8", errors="replace")
        raise RuntimeError(_explain_error(f"SUB2API HTTP {e.code} {e.reason} {path}: {text[:300]}")) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"SUB2API 网络错误 {path}: {e.reason}") from e

    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError as e:
        raise RuntimeError(f"SUB2API 返回非 JSON：{text[:300]}") from e

    # SUB2API 标准包装：{code, message, data}
    if isinstance(parsed, dict) and "code" in parsed:
        if parsed.get("code") == 0:
            return parsed.get("data")
        detail = parsed.get("message") or parsed.get("detail") or f"SUB2API code={parsed.get('code')} on {path}"
        raise RuntimeError(_explain_error(str(detail)))
    return parsed


def _explain_error(message: str) -> str:
    """Turn the backend's auth error into something the operator can act on."""
    if "INVALID_ADMIN_KEY" in message:
        return f"{message} —— 管理员 API Key 无效或已失效，请在 SUB2API 后台重新生成后再更新设置"
    return message


def auth_headers(target: Sub2ApiTarget) -> Dict[str, str]:
    """Admin credentials for SUB2API, following the upstream admin contract.

    `x-api-key` (admin API key) is preferred; a JWT obtained from the admin
    email/password is only the documented fallback.
    """
    key = (target.admin_api_key or "").strip()
    if key:
        return {"x-api-key": key}
    if (target.email or "").strip() and target.password:
        return {"Authorization": f"Bearer {login(target)}"}
    raise RuntimeError(
        "SUB2API 需要管理员 API Key（请求头 x-api-key）；"
        "如该部署未启用 API Key，可改用管理员邮箱+密码走 JWT 兜底"
    )


def login(target: Sub2ApiTarget) -> str:
    """JWT 兜底：用管理员邮箱密码登录，返回 access_token。"""
    data = _request_json(
        target.origin(), "/api/v1/auth/login",
        method="POST",
        body={"email": target.email, "password": target.password},
        proxy=target.proxy,
        proxy_insecure=target.proxy_insecure,
    )
    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError("SUB2API /auth/login 未返回 access_token")
    return data["access_token"]


def list_groups(target: Sub2ApiTarget, headers: Mapping[str, str]) -> List[Dict[str, Any]]:
    data = _request_json(
        target.origin(), "/api/v1/admin/groups/all",
        method="GET", headers=headers, proxy=target.proxy, proxy_insecure=target.proxy_insecure,
    )
    return data if isinstance(data, list) else []


def resolve_group_id(groups: List[Dict[str, Any]], name: str) -> Optional[int]:
    target = (name or "").strip().lower()
    if not target:
        return None
    for g in groups:
        if str(g.get("name", "")).strip().lower() == target:
            try:
                return int(g.get("id"))
            except (TypeError, ValueError):
                continue
    return None


def create_account(target: Sub2ApiTarget, headers: Mapping[str, str], payload: Dict[str, Any]) -> Dict[str, Any]:
    return _request_json(
        target.origin(), "/api/v1/admin/accounts",
        method="POST", headers=headers, body=payload, proxy=target.proxy, proxy_insecure=target.proxy_insecure,
    ) or {}


# ---------------------------------------------------------------------------
# Push pipeline
# ---------------------------------------------------------------------------

@dataclass
class PushResult:
    pushed: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)


def push_accounts(
    accounts: Iterable[Mapping[str, Any]],
    target: Sub2ApiTarget,
    *,
    skip_empty: bool = True,
    require_codex: bool = False,
    dry_run: bool = False,
    on_progress=None,  # callable(stage, info)
) -> PushResult:
    """鉴权（管理员 API Key 优先）→ 解析 group → 把每个有效账号挨个创建过去。"""
    result = PushResult()

    auth_mode = "x-api-key" if (target.admin_api_key or "").strip() else "jwt"
    if on_progress: on_progress("auth", {"origin": target.origin(), "mode": auth_mode})
    headers = auth_headers(target)
    if on_progress: on_progress("auth_ok", {"mode": auth_mode})

    if on_progress: on_progress("groups", {})
    groups = list_groups(target, headers)
    group_id = resolve_group_id(groups, target.group_name)
    if not group_id:
        names = ", ".join(str(g.get("name", "?")) for g in groups[:20])
        raise RuntimeError(f"分组 {target.group_name!r} 在 SUB2API 中找不到。已有分组：{names}")
    if on_progress: on_progress("groups_ok", {"groupId": group_id, "groupName": target.group_name})

    rows = collect_accounts_from_dir(accounts, skip_empty=skip_empty, require_codex=require_codex)
    for label, account in rows:
        try:
            payload = build_payload_from_account(
                account,
                concurrency=target.concurrency,
                priority=target.priority,
                rate_multiplier=target.rate_multiplier,
                auto_pause_on_expired=target.auto_pause_on_expired,
                privacy_mode=target.privacy_mode,
                group_ids=[group_id],
            )
            # SUB2API create 接口要 notes 字段（即使是空）
            payload.setdefault("notes", "")
        except Exception as e:  # noqa: BLE001
            result.skipped.append({"email": label, "reason": str(e)})
            if on_progress: on_progress("skip", {"email": label, "reason": str(e)})
            continue

        if dry_run:
            result.pushed.append({"email": label, "name": payload["name"], "dryRun": True})
            if on_progress: on_progress("dry", {"email": label})
            continue

        try:
            created = create_account(target, headers, payload)
            result.pushed.append({
                "email": label,
                "name": payload["name"],
                "id": created.get("id") if isinstance(created, dict) else None,
            })
            if on_progress: on_progress("ok", {"email": label, "id": (created or {}).get("id")})
        except Exception as e:  # noqa: BLE001
            result.failed.append({"email": label, "name": payload["name"], "error": str(e)})
            if on_progress: on_progress("fail", {"email": label, "error": str(e)})

    if on_progress: on_progress("done", {
        "pushed": len(result.pushed),
        "failed": len(result.failed),
        "skipped": len(result.skipped),
    })
    return result
