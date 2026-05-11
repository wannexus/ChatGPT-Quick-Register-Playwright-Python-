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
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# core.sub2api: 给 SUB2API 后端推账号 / 导出批量 JSON
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import sub2api  # noqa: E402
from core.local_config import CONFIG_PATH, effective_config, save_config  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "output"
STATIC_DIR = Path(__file__).resolve().parent / "static"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Batch process manager (single concurrent run)
# ---------------------------------------------------------------------------
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
            self.recent = [f"$ {' '.join(cmd)}"]
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


def _summarize(account_path: Path) -> Optional[dict]:
    try:
        data = json.loads(account_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    session = data.get("session") or {}
    user = session.get("user") if isinstance(session, dict) else {}
    user = user if isinstance(user, dict) else {}
    return {
        "filename": account_path.name,
        "email": data.get("email") or "",
        "savedAt": data.get("savedAt"),
        "sessionFetchOk": bool(data.get("sessionFetchOk")),
        "expires": session.get("expires"),
        "hasAccessToken": bool(session.get("accessToken")),
        "userEmail": user.get("email"),
        "authMode": data.get("authMode"),
        "emailSource": data.get("emailSource"),
        "codeSource": data.get("codeSource"),
        "reloginRequired": bool(data.get("reloginRequired")),
        "reloginReason": data.get("reloginReason") or "",
        "payurlCheck": data.get("payurlCheck") or {},
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
        "qqUserPresent": bool(cfg.get("qqUser")),
        "qqPassPresent": bool(cfg.get("qqPass")),
        "duckToken": cfg.get("duckToken") or "",
        "duckBaseUrl": cfg.get("duckBaseUrl") or "",
        "qqUser": cfg.get("qqUser") or "",
        "qqPass": cfg.get("qqPass") or "",
        "proxy": cfg.get("proxy") or "",
        "configPath": str(CONFIG_PATH),
    }


class LocalConfigPayload(BaseModel):
    duckToken: str = ""
    duckBaseUrl: str = ""
    qqUser: str = ""
    qqPass: str = ""
    proxy: str = ""


@app.post("/api/defaults")
async def update_defaults(payload: LocalConfigPayload):
    saved = save_config(payload.model_dump())
    return {
        "ok": True,
        "configPath": str(CONFIG_PATH),
        "duckTokenPresent": bool(saved.get("duckToken")),
        "qqUserPresent": bool(saved.get("qqUser")),
        "qqPassPresent": bool(saved.get("qqPass")),
    }


# ---------------- Accounts ----------------
@app.get("/api/accounts")
async def list_accounts():
    rows = []
    for f in sorted(OUTPUT_DIR.glob("*.json")):
        item = _summarize(f)
        if item:
            rows.append(item)
    return rows


def _resolve_account_path(filename: str) -> Optional[Path]:
    """防路径穿越：必须是 OUTPUT_DIR 下、.json 后缀、且实际存在。"""
    candidate = (OUTPUT_DIR / filename).resolve()
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError:
        return None
    if candidate.suffix != ".json" or not candidate.is_file():
        return None
    return candidate


@app.get("/api/accounts/{filename}")
async def get_account(filename: str):
    target = _resolve_account_path(filename)
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return data


@app.delete("/api/accounts/{filename}")
async def delete_account(filename: str):
    target = _resolve_account_path(filename)
    if target is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    target.unlink()
    return {"ok": True, "filename": filename}


@app.post("/api/accounts/cleanup")
async def cleanup_empty():
    removed: List[str] = []
    for f in OUTPUT_DIR.glob("*.json"):
        item = _summarize(f)
        if item and not item.get("hasAccessToken"):
            try:
                f.unlink()
                removed.append(f.name)
            except Exception:
                pass
    return {"removed": removed, "count": len(removed)}


@app.post("/api/accounts/delete-relogin-required")
async def delete_relogin_required():
    removed: List[str] = []
    failed: List[dict] = []
    for f in sorted(OUTPUT_DIR.glob("*.json")):
        item = _summarize(f)
        if not item or not item.get("reloginRequired"):
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except Exception as e:  # noqa: BLE001
            failed.append({"filename": f.name, "error": str(e)})
    _rebuild_relogin_required_index(OUTPUT_DIR)
    return {"ok": True, "removed": removed, "failed": failed, "count": len(removed)}


class ReloginPayload(BaseModel):
    codeSource: str = "qq"
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None
    proxy: str = ""
    headless: bool = False
    noPersistent: bool = False
    noClearTokens: bool = False
    stopOnError: bool = False
    cooldown: int = 10
    runCodexOauth: bool = False
    reloginLimit: int = 1
    reloginTimeout: int = 180


def _count_relogin_required() -> int:
    count = 0
    for f in OUTPUT_DIR.glob("*.json"):
        item = _summarize(f)
        if item and item.get("reloginRequired"):
            count += 1
    return count


def _build_relogin_args(p: ReloginPayload) -> List[str]:
    args: List[str] = [
        "--mode", "relogin",
        "--relogin-marked",
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
    if p.headless: args.append("--headless")
    if p.noPersistent: args.append("--no-persistent")
    if p.noClearTokens: args.append("--no-clear-tokens")
    if p.stopOnError: args.append("--stop-on-error")
    if p.runCodexOauth:
        args.append("--codex-oauth")
    else:
        args.append("--no-codex-oauth")
    return args


@app.post("/api/accounts/relogin")
async def relogin_marked(payload: ReloginPayload):
    if payload.codeSource != "qq":
        return JSONResponse({"ok": False, "error": "一键重登需要 code-source=qq 自动收码"}, status_code=400)
    marked_count = _count_relogin_required()
    if marked_count <= 0:
        return JSONResponse({"ok": False, "error": "没有需要重登的账号"}, status_code=400)
    args = _build_relogin_args(payload)
    ok, msg = await manager.start(args)
    if not ok:
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    return {"ok": True, "markedCount": marked_count, "args": args}


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
    qqUser: str = ""
    qqPass: str = ""
    qqMaxAttempts: Optional[int] = None
    qqInterval: Optional[float] = None
    proxy: str = ""
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
    if p.qqUser: args += ["--qq-user", p.qqUser]
    if p.qqPass: args += ["--qq-pass", p.qqPass]
    if p.qqMaxAttempts is not None: args += ["--qq-max-attempts", str(p.qqMaxAttempts)]
    if p.qqInterval is not None: args += ["--qq-interval", str(p.qqInterval)]
    if p.proxy: args += ["--proxy", p.proxy]
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
    return {"ok": True, "args": args}


@app.post("/api/batch/stop")
async def batch_stop():
    stopped = await manager.stop()
    return {"ok": True, "stopped": stopped}


@app.get("/api/batch/status")
async def batch_status():
    return {"running": manager.is_running}


# ---------------- SUB2API export / push ----------------
class Sub2ApiCommon(BaseModel):
    groupName: str = "codex"
    groupId: Optional[int] = None
    concurrency: int = 5
    priority: int = 1
    rateMultiplier: int = 1
    autoPauseOnExpired: bool = True
    privacyMode: str = "training_off"
    skipEmpty: bool = True
    requireCodex: bool = True   # 默认只导出有完整 codexAuth 的账号


class Sub2ApiPushPayload(Sub2ApiCommon):
    baseUrl: str
    email: str
    password: str
    proxy: str = ""
    dryRun: bool = False


def _build_export(payload: Sub2ApiCommon, *, include_group: bool = False) -> dict:
    """include_group=True 时（push 走的路径）会带 group_ids；export 不需要。"""
    kwargs = dict(
        out_dir=OUTPUT_DIR,
        concurrency=payload.concurrency,
        priority=payload.priority,
        rate_multiplier=payload.rateMultiplier,
        auto_pause_on_expired=payload.autoPauseOnExpired,
        privacy_mode=payload.privacyMode,
        skip_empty=payload.skipEmpty,
        require_codex=payload.requireCodex,
    )
    if include_group and payload.groupId:
        kwargs["group_ids"] = [payload.groupId]
    return sub2api.build_export_bundle(**kwargs)


def _truncate_creds_for_preview(p: dict) -> dict:
    c = dict(p.get("credentials") or {})
    for k in ("access_token", "id_token", "refresh_token"):
        if c.get(k):
            c[k] = c[k][:24] + "..."
    return {**p, "credentials": c}


@app.post("/api/sub2api/preview")
async def sub2api_preview(payload: Sub2ApiCommon):
    """返回预览：count + skipped + 前 3 条样例 payload。"""
    bundle = _build_export(payload)
    meta = bundle.get("_meta", {})
    sample = [_truncate_creds_for_preview(p) for p in bundle["accounts"][:3]]
    return {
        "count": meta.get("count", len(bundle["accounts"])),
        "skipped": meta.get("skipped", []),
        "sample": sample,
        "exportedAt": bundle.get("exported_at"),
    }


@app.post("/api/sub2api/export")
async def sub2api_export(payload: Sub2ApiCommon):
    """返回完整 JSON 文件下载——结构 {exported_at, proxies, accounts}，不含 _meta。"""
    bundle = _build_export(payload)
    bundle.pop("_meta", None)
    text = json.dumps(bundle, ensure_ascii=False, indent=2)
    fname = f"sub2api-batch-{len(bundle.get('accounts', []))}.json"
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/sub2api/push")
async def sub2api_push(payload: Sub2ApiPushPayload):
    """登录 SUB2API → 找分组 → 批量创建账号。"""
    target = sub2api.Sub2ApiTarget(
        base_url=payload.baseUrl,
        email=payload.email,
        password=payload.password,
        group_name=payload.groupName,
        concurrency=payload.concurrency,
        priority=payload.priority,
        rate_multiplier=payload.rateMultiplier,
        auto_pause_on_expired=payload.autoPauseOnExpired,
        privacy_mode=payload.privacyMode,
        proxy=payload.proxy or None,
    )

    # urllib 是同步的，丢到线程池
    loop = asyncio.get_event_loop()

    progress_log: List[str] = []
    def cb(stage, info):
        msg = f"[sub2api] {stage}  {info}"
        progress_log.append(msg)
        # 顺便往日志面板广播一份
        try:
            asyncio.run_coroutine_threadsafe(manager._broadcast(msg), loop)
        except Exception:
            pass

    try:
        result = await loop.run_in_executor(
            None,
            lambda: sub2api.push_directory(
                OUTPUT_DIR, target,
                skip_empty=payload.skipEmpty,
                require_codex=payload.requireCodex,
                dry_run=payload.dryRun,
                on_progress=cb,
            ),
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e), "log": progress_log}, status_code=400)

    return {
        "ok": True,
        "pushed": result.pushed,
        "failed": result.failed,
        "skipped": result.skipped,
        "log": progress_log,
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
