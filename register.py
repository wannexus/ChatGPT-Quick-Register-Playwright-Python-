"""ChatGPT Quick Register — Playwright CLI.

Run examples:
    # 单个：DuckDuckGo API + QQ IMAP 自动收码
    python register.py --email-source duck-api --code-source qq

    # 批量 10 个，每个之间冷却 30 秒（避免 OpenAI 风控）
    python register.py --email-source duck-api --code-source qq --count 10 --cooldown 30

    # 全手动（自己填邮箱、终端贴验证码）
    python register.py --email me@example.com
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import json
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

from core import flow, session
from core import codex_oauth as codex_oauth_module
from core.local_config import effective_config
from core.duck import fetch_duck_email
from core.duck_api import generate_private_address as duck_api_generate
from core.qq_imap import QQImapConfig, fetch_qq_code
from data.names import generate_password

DEFAULT_USER_DATA_DIR = Path.home() / ".chatgpt_quick_register" / "profile"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "output"
LOCAL_CONFIG = effective_config()


def _profile_in_use(user_data_dir: Path) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-f", str(user_data_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return False
    current = str(os.getpid())
    return any(pid.strip() and pid.strip() != current for pid in result.stdout.splitlines())


def _clear_stale_profile_locks(user_data_dir: Path) -> None:
    lock_files = [
        user_data_dir / "SingletonLock",
        user_data_dir / "SingletonCookie",
        user_data_dir / "SingletonSocket",
    ]
    existing = [p for p in lock_files if p.exists() or p.is_symlink()]
    if not existing:
        return
    if _profile_in_use(user_data_dir):
        names = ", ".join(p.name for p in existing)
        raise RuntimeError(
            f"浏览器 profile 正在被占用：{user_data_dir} ({names})。"
            "请关闭当前自动化 Chromium，或勾选 no-persistent/换 user-data-dir 后再试。"
        )
    for path in existing:
        try:
            path.unlink()
            print(f"[profile] removed stale {path.name}")
        except FileNotFoundError:
            pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="register.py",
        description="跑完 ChatGPT 注册的 1-6 步，把账号 + /api/auth/session 写到 <邮箱>.json",
    )
    p.add_argument("--mode", choices=["register", "relogin"], default="register", help="运行模式：register=注册新账号；relogin=重登已有账号")
    p.add_argument("--email", default="", help="注册邮箱（email-source=manual 时必填）")
    p.add_argument("--password", default="", help="注册密码（仅 auth-mode=password 时使用，留空自动生成）")
    p.add_argument(
        "--auth-mode", choices=["otp", "password"], default="otp",
        help="注册方式：otp=不设密码、用一次性验证码（默认）；password=填密码注册",
    )
    p.add_argument(
        "--email-source", choices=["manual", "duck", "duck-api"], default="manual",
        help="邮箱来源：manual=用 --email；duck=Playwright 打开 DDG autofill 页生成；"
             "duck-api=直接调 DDG Email Protection API（需要 --duck-token）",
    )
    p.add_argument(
        "--duck-token", default=LOCAL_CONFIG.get("duckToken", ""),
        help="DuckDuckGo Email Protection auth_token（duck-api 模式必填，可走环境变量 QR_DUCK_TOKEN）",
    )
    p.add_argument(
        "--duck-base-url", default=LOCAL_CONFIG.get("duckBaseUrl") or "https://quack.duckduckgo.com",
        help="DuckDuckGo Email API base URL（默认 https://quack.duckduckgo.com）",
    )
    p.add_argument(
        "--code-source", choices=["manual", "qq"], default="manual",
        help="验证码来源：manual=终端手输；qq=用 IMAP 拉 QQ 邮箱",
    )
    p.add_argument("--qq-user", default=LOCAL_CONFIG.get("qqUser", ""), help="QQ 邮箱地址")
    p.add_argument("--qq-pass", default=LOCAL_CONFIG.get("qqPass", ""), help="QQ 邮箱 IMAP 授权码")
    p.add_argument("--qq-host", default="imap.qq.com", help="IMAP host（默认 imap.qq.com）")
    p.add_argument("--qq-port", type=int, default=993, help="IMAP port（默认 993）")
    p.add_argument("--qq-max-attempts", type=int, default=90)
    p.add_argument("--qq-interval", type=float, default=1.5)
    p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="输出目录（默认 ./output）")
    p.add_argument(
        "--user-data-dir", default=str(DEFAULT_USER_DATA_DIR),
        help=f"持久化浏览器 profile 路径（默认 {DEFAULT_USER_DATA_DIR}）",
    )
    p.add_argument("--headless", action="store_true", help="以无头模式运行（不推荐：易触发 Cloudflare）")
    p.add_argument("--no-persistent", action="store_true", help="不使用持久化 profile（每次都干净环境）")
    p.add_argument(
        "--no-clear-tokens", action="store_true",
        help="启动时**不要**清除 OpenAI / ChatGPT 的 cookies/localStorage。默认会清",
    )
    p.add_argument(
        "--proxy", default=LOCAL_CONFIG.get("proxy", ""),
        help="HTTP/SOCKS 代理，例如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080",
    )
    p.add_argument(
        "--nav-timeout", type=int, default=int(os.environ.get("QR_NAV_TIMEOUT", "60")),
        help="页面跳转超时秒数（默认 60，DDG 慢可调到 120）",
    )
    p.add_argument(
        "--count", type=int, default=1,
        help="批量注册的账号数（默认 1）。manual 邮箱来源不支持 count > 1",
    )
    p.add_argument(
        "--cooldown", type=int, default=30,
        help="批量注册时每个账号之间的冷却秒数（默认 30，避免 OpenAI 风控）",
    )
    p.add_argument(
        "--stop-on-error", action="store_true",
        help="批量模式下任一账号失败就停止（默认会跳过失败账号继续下一个）",
    )
    p.add_argument(
        "--no-codex-oauth", action="store_true",
        help="跳过 codex OAuth 流（默认已跳过；保留此参数用于兼容旧命令）",
    )
    p.add_argument(
        "--codex-oauth", action="store_true",
        help="注册成功后再跑一次 OAuth 拿完整 codex 凭据，包含 refresh_token 用于 SUB2API",
    )
    p.add_argument("--relogin-marked", action="store_true", help="重登 output/*.json 中 reloginRequired=true 的账号")
    p.add_argument("--relogin-file", action="append", default=[], help="重登指定账号 JSON 文件名或路径，可重复传入")
    p.add_argument("--relogin-limit", type=int, default=0, help="最多重登多少个账号（0=不限制）")
    p.add_argument(
        "--relogin-timeout",
        type=int,
        default=int(os.environ.get("QR_RELOGIN_TIMEOUT", "180")),
        help="单个账号重登总超时秒数（默认 180）",
    )
    return p.parse_args(argv)


async def _input_async(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return await loop.run_in_executor(pool, input, prompt)


def make_code_fetcher(args: argparse.Namespace, *, since_ts: float):
    if args.code_source == "qq":
        if not args.qq_user or not args.qq_pass:
            raise SystemExit("--code-source=qq 需要同时提供 --qq-user 和 --qq-pass（IMAP 授权码）")
        config = QQImapConfig(
            user=args.qq_user,
            password=args.qq_pass,
            host=args.qq_host,
            port=args.qq_port,
        )

        async def fetch():
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                return await loop.run_in_executor(
                    pool,
                    lambda: fetch_qq_code(
                        config,
                        max_attempts=args.qq_max_attempts,
                        interval_seconds=args.qq_interval,
                        since_ts=since_ts,
                    ),
                )
        return fetch

    async def fetch_manual():
        return await _input_async("请输入收到的 6 位验证码: ")

    return fetch_manual


@dataclass
class RunResult:
    email: str
    status: str          # "ok" | "fail"
    filename: str = ""
    error: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _looks_logged_in_session(session_result: dict | None) -> bool:
    parsed = (session_result or {}).get("parsed") or {}
    if not isinstance(parsed, dict):
        return False
    if parsed.get("accessToken"):
        return True
    user = parsed.get("user")
    return isinstance(user, dict) and bool(user.get("email") or user.get("id"))


def _resolve_relogin_file(out_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = out_dir / path
    path = path.resolve()
    try:
        path.relative_to(out_dir.resolve())
    except ValueError as e:
        raise SystemExit(f"--relogin-file 必须位于输出目录内：{value}") from e
    if path.suffix != ".json" or not path.is_file():
        raise SystemExit(f"重登账号文件不存在或不是 JSON：{value}")
    return path


def _load_relogin_targets(args: argparse.Namespace, out_dir: Path) -> list[Path]:
    targets: list[Path] = []
    seen: set[Path] = set()
    for value in args.relogin_file or []:
        path = _resolve_relogin_file(out_dir, value)
        if path not in seen:
            targets.append(path)
            seen.add(path)
    if args.relogin_marked:
        for path in sorted(out_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if data.get("reloginRequired") and path not in seen:
                targets.append(path)
                seen.add(path)
    if args.relogin_limit and args.relogin_limit > 0:
        targets = targets[:args.relogin_limit]
    return targets


def _rebuild_relogin_required_index(out_dir: Path) -> None:
    accounts = []
    for path in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data.get("reloginRequired"):
            continue
        accounts.append({
            "email": data.get("email") or path.stem,
            "file": path.name,
            "path": str(path),
            "reason": data.get("reloginReason") or data.get("reloginLastError") or "",
            "marked_at": data.get("reloginMarkedAt") or data.get("reloginLastAttemptAt") or "",
            "status": "pending_relogin",
        })
    target = out_dir.parent / "relogin_required.json"
    target.write_text(json.dumps({
        "updated_at": _utc_now(),
        "count": len(accounts),
        "accounts": accounts,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def _run_one_account(
    args: argparse.Namespace,
    context,
    *,
    out_dir: Path,
    label: str,
    nav_timeout_ms: int,
) -> RunResult:
    """单个账号注册。失败抛错由调用方处理。"""
    page = await context.new_page()
    since_ts = time.time()
    email = ""
    try:
        # 1) 清旧账号 cookie / storage
        if not args.no_clear_tokens:
            await flow.clear_openai_state(context, also_storage=True)

        # 2) 决定邮箱
        if args.email_source == "duck-api":
            email = duck_api_generate(
                args.duck_token,
                base_url=args.duck_base_url,
                proxy=args.proxy or None,
            )
            print(f"[{label}] duck email -> {email}")
        elif args.email_source == "duck":
            duck_page = await context.new_page()
            try:
                email = await fetch_duck_email(duck_page, generate_new=True, nav_timeout_ms=nav_timeout_ms)
            finally:
                try:
                    await duck_page.close()
                except Exception:
                    pass
            print(f"[{label}] duck email -> {email}")
        else:
            email = args.email.strip()
            if not email:
                raise SystemExit("--email-source=manual 需要 --email")

        # 3) 决定密码
        if args.auth_mode == "password":
            password = args.password.strip() or generate_password()
        else:
            password = args.password.strip()
        print(f"[{label}] password={password or '<otp>'}")

        # 4) 6 步流程
        await flow.step1_open(page)
        await flow.step2_signup_email(page, email)
        actual_password = await flow.step3_password(page, password, auth_mode=args.auth_mode)

        fetch_code = make_code_fetcher(args, since_ts=since_ts)
        code = await flow.step4_code(page, fetch_code)

        first_name, last_name, birthday = flow.random_profile()
        await flow.step5_profile(page, first_name=first_name, last_name=last_name, birthday=birthday)

        await flow.step6_wait_success(page)

        # 5) 抓 session（带轮询，确保拿到非空 session）—— 这一步给 Plus 订阅用
        session_result = None
        try:
            session_result = await session.fetch_session(page)
            parsed = (session_result or {}).get("parsed") or {}
            has_token = bool(parsed.get("accessToken"))
            has_user = bool((parsed.get("user") or {}).get("email"))
            if has_token or has_user:
                expires = parsed.get("expires", "?")
                print(f"[{label}] [session] OK status={session_result['status']}  expires={expires}  hasAccessToken={has_token}")
            else:
                print(f"[{label}] [session] WARN status={session_result.get('status')}  body 为空（auth cookie 未落地，文件依然会写）")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] [session] fetch failed: {e}")

        # 6) 跑 codex OAuth 拿完整凭据（refresh_token + id_token）— 给 SUB2API 用
        # OAuth 阶段会再触发一次 OTP 邮件，需要新的 since_ts 避免拿到注册阶段那封旧邮件
        codex_creds = None
        if args.codex_oauth and not args.no_codex_oauth:
            try:
                oauth_since_ts = time.time()
                oauth_fetch_code = make_code_fetcher(args, since_ts=oauth_since_ts)
                codex_creds = await codex_oauth_module.run_codex_oauth(
                    page,
                    account_email=email,
                    fetch_code=oauth_fetch_code,
                    proxy=args.proxy or None,
                    timeout=300.0,
                )
                print(f"[{label}] [codex-oauth] OK  plan={codex_creds.get('plan_type')}  "
                      f"refresh_token={'yes' if codex_creds.get('refresh_token') else 'NO'}")
            except Exception as e:  # noqa: BLE001
                print(f"[{label}] [codex-oauth] FAILED: {e}")
        else:
            print(f"[{label}] [codex-oauth] skipped（未启用 --codex-oauth）")

        extras = {
            "authMode": args.auth_mode,
            "emailSource": args.email_source,
            "codeSource": args.code_source,
            "verificationCode": code,
            "name": {"first": first_name, "last": last_name},
            "birthday": birthday.iso(),
            "codexAuth": codex_creds,
        }
        target = session.save_account_snapshot(
            out_dir, email, actual_password,
            session_result=session_result, extras=extras,
        )
        print(f"[{label}] [done] 写入 {target}")
        return RunResult(email=email, status="ok", filename=str(target))
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _run_one_relogin(
    args: argparse.Namespace,
    context,
    account_path: Path,
    *,
    out_dir: Path,
    label: str,
) -> RunResult:
    page = await context.new_page()
    since_ts = time.time()
    try:
        data = json.loads(account_path.read_text(encoding="utf-8"))
        email = (data.get("email") or "").strip()
        if not email:
            raise RuntimeError("账号文件缺少 email")
        password = (data.get("password") or "").strip()
        auth_mode = data.get("authMode") or ("password" if password else "otp")
        previous_reason = data.get("reloginReason") or ""
        previous_marked_at = data.get("reloginMarkedAt") or ""

        if not args.no_clear_tokens:
            await flow.clear_openai_state(context, also_storage=True)

        print(f"[{label}] [relogin] 开始：{email}  auth={auth_mode}")
        fetch_code = make_code_fetcher(args, since_ts=since_ts)
        code = await asyncio.wait_for(
            flow.step_login_existing_account(
                page,
                email=email,
                password=password,
                auth_mode=auth_mode,
                fetch_code=fetch_code,
                total_timeout_seconds=max(45, min(args.relogin_timeout, 180)),
            ),
            timeout=max(60, args.relogin_timeout),
        )

        session_result = await session.fetch_session(
            page,
            total_timeout=min(30.0, max(10.0, args.relogin_timeout / 6)),
            reload_after_seconds=8.0,
            second_reload_after_seconds=18.0,
        )
        if not _looks_logged_in_session(session_result):
            raise RuntimeError("登录后未拿到有效 session")

        codex_creds = data.get("codexAuth")
        if args.codex_oauth and not args.no_codex_oauth:
            try:
                oauth_fetch_code = make_code_fetcher(args, since_ts=time.time())
                codex_creds = await codex_oauth_module.run_codex_oauth(
                    page,
                    account_email=email,
                    fetch_code=oauth_fetch_code,
                    proxy=args.proxy or None,
                    timeout=300.0,
                )
                print(f"[{label}] [codex-oauth] OK  refresh_token={'yes' if codex_creds.get('refresh_token') else 'NO'}")
            except Exception as e:  # noqa: BLE001
                print(f"[{label}] [codex-oauth] FAILED: {e}")
        else:
            print(f"[{label}] [codex-oauth] skipped（未启用 --codex-oauth）")

        extras = {
            "authMode": auth_mode,
            "emailSource": data.get("emailSource"),
            "codeSource": args.code_source,
            "verificationCode": code,
            "name": data.get("name"),
            "birthday": data.get("birthday"),
            "codexAuth": codex_creds,
            "reloginRequired": False,
            "reloginLastOkAt": _utc_now(),
            "reloginCodeSource": args.code_source,
        }
        if previous_reason:
            extras["reloginPreviousReason"] = previous_reason
        if previous_marked_at:
            extras["reloginPreviousMarkedAt"] = previous_marked_at
        for key in list(extras):
            if extras[key] is None:
                del extras[key]

        target = session.save_account_snapshot(out_dir, email, password, session_result=session_result, extras=extras)
        print(f"[{label}] [relogin] ✓ 写入 {target}")
        return RunResult(email=email, status="ok", filename=str(target))
    except Exception as e:  # noqa: BLE001
        err_msg = str(e) or e.__class__.__name__
        try:
            data = json.loads(account_path.read_text(encoding="utf-8"))
            data["reloginRequired"] = True
            data["reloginLastAttemptAt"] = _utc_now()
            data["reloginLastError"] = err_msg
            account_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        raise
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _open_browser_context(args: argparse.Namespace, nav_timeout_ms: int):
    pw = await async_playwright().start()
    browser_args = {
        "headless": args.headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if args.proxy:
        browser_args["proxy"] = {"server": args.proxy}

    user_data_dir = Path(args.user_data_dir).expanduser()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    if args.no_persistent:
        browser = await pw.chromium.launch(**browser_args)
        context = await browser.new_context()
    else:
        _clear_stale_profile_locks(user_data_dir)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            **browser_args,
        )
        browser = None
    context.set_default_navigation_timeout(nav_timeout_ms)
    context.set_default_timeout(nav_timeout_ms)
    return pw, browser, context


async def _close_browser_context(pw, browser, context) -> None:
    try:
        if browser is not None:
            await context.close()
            await browser.close()
        else:
            await context.close()
    finally:
        await pw.stop()


async def run_register(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 早期参数校验
    if args.email_source == "manual" and not args.email.strip():
        raise SystemExit("--email-source=manual 需要 --email")
    if args.email_source == "duck-api" and not args.duck_token:
        raise SystemExit("--email-source=duck-api 需要 --duck-token（或环境变量 QR_DUCK_TOKEN）")
    if args.count > 1 and args.email_source == "manual":
        raise SystemExit("--count > 1 不支持 manual 邮箱来源（每个账号需要不同邮箱）；改用 duck / duck-api")
    if args.count < 1:
        raise SystemExit("--count 必须 ≥ 1")

    nav_timeout_ms = max(10, args.nav_timeout) * 1000

    print(f"[init] count={args.count}  cooldown={args.cooldown}s")
    print(f"[init] email-source={args.email_source}  code-source={args.code_source}")
    print(f"[init] auth-mode={args.auth_mode}  out={out_dir}")
    if args.proxy:
        print(f"[init] proxy={args.proxy}")

    pw, browser, context = await _open_browser_context(args, nav_timeout_ms)
    try:
        results: list[RunResult] = []
        try:
            for i in range(args.count):
                label = f"{i + 1}/{args.count}"
                print(f"\n========== [{label}] 开始注册 ==========")
                try:
                    result = await _run_one_account(
                        args, context,
                        out_dir=out_dir,
                        label=label,
                        nav_timeout_ms=nav_timeout_ms,
                    )
                    results.append(result)
                    print(f"========== [{label}] ✓ 成功：{result.email} ==========")
                except KeyboardInterrupt:
                    raise
                except SystemExit:
                    raise
                except Exception as e:  # noqa: BLE001
                    err_msg = str(e) or e.__class__.__name__
                    print(f"========== [{label}] ✗ 失败：{err_msg} ==========")
                    if not args.stop_on_error:
                        # 把 trace 落到日志便于排查，但继续下一个
                        traceback.print_exc()
                    results.append(RunResult(email="?", status="fail", error=err_msg))
                    if args.stop_on_error:
                        raise

                # 冷却（最后一个之后不用等）
                if i < args.count - 1 and args.cooldown > 0:
                    print(f"... 冷却 {args.cooldown}s 再开始下一个 ...")
                    await asyncio.sleep(args.cooldown)
        finally:
            pass

        # ===== 汇总 =====
        ok = [r for r in results if r.status == "ok"]
        fail = [r for r in results if r.status == "fail"]
        print()
        print(f"========== 批量结束：成功 {len(ok)} / 失败 {len(fail)} / 总计 {len(results)} ==========")
        for r in ok:
            print(f"  ✓ {r.email}  ->  {r.filename}")
        for r in fail:
            print(f"  ✗ {r.email}  失败：{r.error}")

        return 0 if not fail or not args.stop_on_error else 1
    finally:
        await _close_browser_context(pw, browser, context)


async def run_relogin(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.relogin_marked and not args.relogin_file:
        raise SystemExit("--mode=relogin 需要 --relogin-marked 或 --relogin-file")
    if args.code_source == "qq" and (not args.qq_user or not args.qq_pass):
        raise SystemExit("--code-source=qq 需要同时提供 --qq-user 和 --qq-pass（IMAP 授权码）")

    targets = _load_relogin_targets(args, out_dir)
    if not targets:
        print("[relogin] 没有找到需要重登的账号")
        _rebuild_relogin_required_index(out_dir)
        return 0

    nav_timeout_ms = max(10, args.nav_timeout) * 1000
    print(f"[init] mode=relogin  targets={len(targets)}  cooldown={args.cooldown}s")
    print(f"[init] code-source={args.code_source}  out={out_dir}")
    if args.proxy:
        print(f"[init] proxy={args.proxy}")

    pw, browser, context = await _open_browser_context(args, nav_timeout_ms)
    results: list[RunResult] = []
    try:
        for i, path in enumerate(targets):
            label = f"{i + 1}/{len(targets)}"
            print(f"\n========== [{label}] 开始重登 {path.name} ==========")
            try:
                result = await _run_one_relogin(args, context, path, out_dir=out_dir, label=label)
                results.append(result)
                print(f"========== [{label}] ✓ 重登成功：{result.email} ==========")
            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001
                err_msg = str(e) or e.__class__.__name__
                print(f"========== [{label}] ✗ 重登失败：{err_msg} ==========")
                if not args.stop_on_error:
                    traceback.print_exc()
                results.append(RunResult(email=path.stem, status="fail", filename=str(path), error=err_msg))
                if args.stop_on_error:
                    raise
            finally:
                _rebuild_relogin_required_index(out_dir)

            if i < len(targets) - 1 and args.cooldown > 0:
                print(f"... 冷却 {args.cooldown}s 再重登下一个 ...")
                await asyncio.sleep(args.cooldown)
    finally:
        await _close_browser_context(pw, browser, context)

    ok = [r for r in results if r.status == "ok"]
    fail = [r for r in results if r.status == "fail"]
    print()
    print(f"========== 重登结束：成功 {len(ok)} / 失败 {len(fail)} / 总计 {len(results)} ==========")
    for r in ok:
        print(f"  ✓ {r.email}  ->  {r.filename}")
    for r in fail:
        print(f"  ✗ {r.email}  失败：{r.error}")
    return 0 if not fail or not args.stop_on_error else 1


async def run(args: argparse.Namespace) -> int:
    if args.mode == "relogin":
        return await run_relogin(args)
    return await run_register(args)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import warnings
    warnings.filterwarnings("ignore", category=ResourceWarning)
    sys.unraisablehook = lambda _u: None  # type: ignore[assignment]

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[abort] 用户中断")
        return 130
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
