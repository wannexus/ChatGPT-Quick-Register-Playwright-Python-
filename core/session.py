"""Fetch chatgpt.com /api/auth/session and persist the account snapshot."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Page

CHATGPT_HOME = "https://chatgpt.com/"

_FETCH_JS = """
async () => {
  try {
    const response = await fetch('/api/auth/session', { credentials: 'include' });
    const text = await response.text();
    let parsed = null;
    try { parsed = JSON.parse(text); } catch (_) { parsed = null; }
    return { ok: response.ok, status: response.status, text, parsed };
  } catch (error) {
    return { ok: false, status: 0, text: '', parsed: null, error: String(error && error.message || error) };
  }
}
"""


def sanitize_filename_segment(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\s]+", "_", value).strip("_")
    return cleaned or "unknown"


def _looks_logged_in(parsed: Any) -> bool:
    if not isinstance(parsed, dict) or not parsed:
        return False
    if parsed.get("accessToken"):
        return True
    user = parsed.get("user")
    if isinstance(user, dict) and (user.get("email") or user.get("id")):
        return True
    return False


async def _fetch_once(page: Page) -> dict[str, Any]:
    return await page.evaluate(_FETCH_JS)


async def fetch_session(
    page: Page,
    *,
    total_timeout: float = 60.0,
    poll_interval: float = 2.0,
    reload_after_seconds: float = 10.0,
    second_reload_after_seconds: float = 30.0,
) -> dict[str, Any]:
    """轮询 `/api/auth/session` 直到拿到非空 session（含 accessToken / user）。

    刚 OAuth 完成时 chatgpt.com 的 auth cookie 可能还没完全落地，瞬时调用会拿
    到 200 但 body 为 `{}`；这里：
    - 总共最多轮询 60 秒（每 2 秒）
    - 中途 10 秒、30 秒各 reload 一次页面，强制重新拿 session cookie
    """
    start = asyncio.get_event_loop().time()
    deadline = start + total_timeout
    first_reload_at = start + reload_after_seconds
    second_reload_at = start + second_reload_after_seconds
    did_first_reload = False
    did_second_reload = False
    last_result: dict[str, Any] = {"ok": False, "status": 0, "text": "", "parsed": None}
    attempts = 0

    while asyncio.get_event_loop().time() < deadline:
        attempts += 1
        try:
            result = await _fetch_once(page)
            last_result = result
            if result.get("ok") and _looks_logged_in(result.get("parsed")):
                print(f"[session] ✓ 第 {attempts} 次轮询拿到非空 session")
                return result
        except Exception as e:  # noqa: BLE001
            last_result = {"ok": False, "status": 0, "text": "", "parsed": None, "error": str(e)}

        async def _reload():
            try:
                if "chatgpt.com" in page.url or "chat.openai.com" in page.url:
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                else:
                    await page.goto(CHATGPT_HOME, wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2)  # 给 cookie 应用一点时间
            except Exception as e:
                print(f"[session] reload 失败（继续轮询）：{e}")

        now = asyncio.get_event_loop().time()
        if not did_first_reload and now >= first_reload_at:
            did_first_reload = True
            print("[session] 10s 仍未拿到，reload 一次页面")
            await _reload()
        elif not did_second_reload and now >= second_reload_at:
            did_second_reload = True
            print("[session] 30s 仍未拿到，再 reload 一次页面")
            await _reload()

        await asyncio.sleep(poll_interval)

    # 全部轮询失败，落 debug 信息
    try:
        url = page.url
    except Exception:
        url = "<unknown>"
    parsed = last_result.get("parsed")
    text = last_result.get("text") or ""
    print(f"[session] ✗ 60s 轮询后仍空。url={url}  status={last_result.get('status')}  body={text[:200]!r}")
    try:
        cookies = await page.context.cookies()
        names = [
            f"{c.get('domain', '?').lstrip('.')}!{c.get('name')}"
            for c in cookies
            if any(d in (c.get('domain', '') or '') for d in ('chatgpt.com', 'openai.com'))
        ]
        print(f"[session] OpenAI/ChatGPT cookies 当前共 {len(names)} 个: {names[:15]}")
    except Exception as e:
        print(f"[session] 列 cookie 失败: {e}")

    return last_result


def save_account_snapshot(
    out_dir: Path,
    email: str,
    password: str,
    *,
    session_result: dict[str, Any] | None,
    extras: dict[str, Any] | None = None,
) -> Path:
    out_dir = Path(out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    snapshot: dict[str, Any] = {
        "email": email,
        "password": password,
        "savedAt": datetime.now(timezone.utc).isoformat(),
        "sessionFetchOk": bool(session_result and session_result.get("ok")),
        "sessionStatus": int((session_result or {}).get("status") or 0),
        "session": (session_result or {}).get("parsed"),
        "sessionRaw": (session_result or {}).get("text") or "",
    }
    err = (session_result or {}).get("error")
    if err:
        snapshot["sessionError"] = err
    if extras:
        snapshot.update(extras)

    target = out_dir / f"{sanitize_filename_segment(email)}.json"
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
