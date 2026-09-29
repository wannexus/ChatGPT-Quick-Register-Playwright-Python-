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
import json
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from playwright.async_api import async_playwright

from core import flow, session
from core.account_store import AccountStore
from core import ban_check
from core import codex_oauth as codex_oauth_module
from core import ac_checker
from core import sub2api
from core.browser_proxy import BrowserProxyBridge, parse_upstream_proxy
from core.http_utils import build_proxy_url, redact_proxy_url
from core.local_config import effective_config
from core.duck import fetch_duck_email
from core.duck_api import generate_private_address as duck_api_generate
from core.icloud import fetch_icloud_hide_my_email
from core import mhjc
from core import qq_imap
from core.qq_imap import QQImapConfig, fetch_qq_code
from data.names import generate_password

def _argv_has(argv: list[str] | None, flag: str) -> bool:
    """True when the user actually passed `flag` (or `flag=value`) on the CLI."""
    for item in argv or []:
        if item == flag or item.startswith(flag + "="):
            return True
    return False


def _proxy_banner(args: argparse.Namespace) -> str:
    """One line the operator can trust about whether a proxy is actually used."""
    if args.proxy:
        return f"proxy={redact_proxy_url(args.proxy)}（默认启用）"
    if getattr(args, "proxy_enabled", False):
        return "proxy=启用但未配置（将直连；请在设置页填写代理主机/端口）"
    if getattr(args, "proxy_disabled", False):
        return "proxy=已关闭（--no-proxy）"
    return "proxy=已关闭（设置页关闭了「启用代理」）"


def _saved_flag(name: str) -> bool:
    """Read a boolean saved setting; absent settings keep the historic default."""
    return str(LOCAL_CONFIG.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(fallback: int, *sources: str) -> int:
    """First parseable value wins; blank/garbage saved values fall back safely."""
    for raw in sources:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            return int(float(text))
        except ValueError:
            continue
    return fallback


def _env_float(fallback: float, *sources: str) -> float:
    for raw in sources:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            return float(text)
        except ValueError:
            continue
    return fallback


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


def _build_playwright_proxy(proxy_value: str) -> dict | None:
    raw = str(proxy_value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.hostname:
            return {"server": raw}
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        proxy = {"server": server}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy
    except Exception:
        return {"server": raw}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="register.py",
        description="跑完 ChatGPT 注册的 1-6 步，把账号 + /api/auth/session 写到 <邮箱>.json",
    )
    p.add_argument("--mode", choices=["register", "relogin", "codex-push", "ban-scan"], default="register", help="运行模式：register=注册新账号；relogin=重登已有账号；codex-push=选定账号自动登录 Codex 并推送 SUB2API；ban-scan=扫描邮箱标记被封禁账号")
    p.add_argument("--email", default="", help="注册邮箱（email-source=manual 时必填）")
    p.add_argument("--password", default="", help="注册密码（仅 auth-mode=password 时使用，留空自动生成）")
    p.add_argument(
        "--auth-mode", choices=["otp", "password"],
        default=LOCAL_CONFIG.get("authMode") or "otp",
        help="注册方式：otp=不设密码、用一次性验证码（默认）；password=填密码注册",
    )
    p.add_argument(
        "--email-source", choices=["manual", "duck", "duck-api", "icloud", "mhjc"],
        default=LOCAL_CONFIG.get("emailSource") or "manual",
        help="邮箱来源：manual=用 --email；duck=Playwright 打开 DDG autofill 页生成；"
             "duck-api=直接调 DDG Email Protection API（需要 --duck-token）；"
             "icloud=用 iCloud Hide My Email 生成/复用隐私邮箱；"
             "mhjc=调 MHJC 临时邮箱 API 创建 @mhjc.edu.kg 邮箱（需要 --mhjc-api-key）",
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
        "--icloud-host", choices=["auto", "icloud.com", "icloud.com.cn"], default=LOCAL_CONFIG.get("icloudHost") or "auto",
        help="iCloud Host：auto / icloud.com / icloud.com.cn（默认 auto）",
    )
    p.add_argument(
        "--icloud-fetch-mode", default=LOCAL_CONFIG.get("icloudFetchMode") or "always-new",
        help="iCloud 隐私邮箱获取策略：reuse-existing/reuse_existing=优先复用已有别名；"
             "always-new/always_new=始终创建新别名（默认）",
    )
    p.add_argument(
        "--icloud-login-timeout", type=int, default=_env_int(300, LOCAL_CONFIG.get("icloudLoginTimeout"), os.environ.get("QR_ICLOUD_LOGIN_TIMEOUT")),
        help="iCloud 未登录时等待你在打开的浏览器里完成登录的秒数（默认 300）",
    )
    p.add_argument(
        "--mhjc-api-key", default=LOCAL_CONFIG.get("mhjcApiKey", ""),
        help="MHJC 临时邮箱 API Key（email-source=mhjc 必填，可走环境变量 QR_MHJC_API_KEY）",
    )
    p.add_argument(
        "--mhjc-api-base", default=LOCAL_CONFIG.get("mhjcApiBase") or mhjc.DEFAULT_API_BASE,
        help=f"MHJC Mail API base URL（默认 {mhjc.DEFAULT_API_BASE}）",
    )
    p.add_argument(
        "--mhjc-username", default=LOCAL_CONFIG.get("mhjcUsername", ""),
        help="自定义 MHJC 邮箱用户名（不含 @域名）；留空则由服务端随机生成",
    )
    p.add_argument(
        "--mhjc-ttl", type=int, default=_env_int(mhjc.DEFAULT_TTL_SECONDS, os.environ.get("QR_MHJC_TTL")),
        help=f"MHJC 邮箱有效期秒数，最大 {mhjc.DEFAULT_TTL_SECONDS}（默认 {mhjc.DEFAULT_TTL_SECONDS}）",
    )
    p.add_argument(
        "--mhjc-imap-host", default=os.environ.get("QR_MHJC_IMAP_HOST") or mhjc.DEFAULT_IMAP_HOST,
        help=f"MHJC IMAP host（默认 {mhjc.DEFAULT_IMAP_HOST}）",
    )
    p.add_argument(
        "--mhjc-imap-port", type=int, default=_env_int(mhjc.DEFAULT_IMAP_PORT, os.environ.get("QR_MHJC_IMAP_PORT")),
        help=f"MHJC IMAP port（默认 {mhjc.DEFAULT_IMAP_PORT}）",
    )
    p.add_argument(
        "--mhjc-max-attempts", type=int, default=_env_int(60, os.environ.get("QR_MHJC_MAX_ATTEMPTS")),
        help="MHJC IMAP 收码最大轮询次数（默认 60）",
    )
    p.add_argument(
        "--mhjc-interval", type=float, default=_env_float(3, os.environ.get("QR_MHJC_INTERVAL")),
        help="MHJC IMAP 收码轮询间隔秒数（默认 3）",
    )
    p.add_argument(
        "--code-source", choices=["manual", "qq", "mhjc"],
        default=LOCAL_CONFIG.get("codeSource") or "manual",
        help="验证码来源：manual=终端手输；qq=用 IMAP 拉 QQ 邮箱；mhjc=用 IMAP 拉上一步创建的 MHJC 邮箱",
    )
    p.add_argument("--qq-user", default=LOCAL_CONFIG.get("qqUser", ""), help="QQ 邮箱地址")
    p.add_argument("--qq-pass", default=LOCAL_CONFIG.get("qqPass", ""), help="QQ 邮箱 IMAP 授权码")
    p.add_argument("--qq-host", default="imap.qq.com", help="IMAP host（默认 imap.qq.com）")
    p.add_argument("--qq-port", type=int, default=993, help="IMAP port（默认 993）")
    p.add_argument("--qq-max-attempts", type=int, default=_env_int(90, LOCAL_CONFIG.get("qqMaxAttempts")))
    p.add_argument("--qq-interval", type=float, default=_env_float(1.5, LOCAL_CONFIG.get("qqInterval")))
    p.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="输出目录（默认 ./output）")
    p.add_argument(
        "--user-data-dir", default=str(DEFAULT_USER_DATA_DIR),
        help=f"持久化浏览器 profile 路径（默认 {DEFAULT_USER_DATA_DIR}）",
    )
    p.add_argument("--headless", action="store_true", default=_saved_flag("headless"), help="以无头模式运行（不推荐：易触发 Cloudflare）")
    p.add_argument("--no-persistent", action="store_true", default=_saved_flag("noPersistent"), help="不使用持久化 profile（每次都干净环境）")
    p.add_argument(
        "--no-clear-tokens", action="store_true", default=_saved_flag("noClearTokens"),
        help="启动时**不要**清除 OpenAI / ChatGPT 的 cookies/localStorage。默认会清",
    )
    p.add_argument(
        "--no-proxy", dest="proxy_disabled", action="store_true",
        help="本次运行不使用代理（默认会使用下方配置的代理）",
    )
    p.add_argument(
        "--proxy", default=LOCAL_CONFIG.get("proxy", ""),
        help="完整代理 URL（也可改用 --proxy-host/--proxy-port/--proxy-user/--proxy-pass）",
    )
    p.add_argument(
        "--proxy-scheme", default=LOCAL_CONFIG.get("proxyScheme", "") or "http",
        help="代理协议：http / https / socks5（默认 http）",
    )
    p.add_argument(
        "--proxy-host", default=LOCAL_CONFIG.get("proxyHost", ""),
        help="代理主机（设置后与端口/账号/密码组合成代理 URL）",
    )
    p.add_argument(
        "--proxy-port", default=LOCAL_CONFIG.get("proxyPort", ""),
        help="代理端口",
    )
    p.add_argument(
        "--proxy-user", default=LOCAL_CONFIG.get("proxyUser", ""),
        help="代理账号（可选）",
    )
    p.add_argument(
        "--proxy-pass", default=LOCAL_CONFIG.get("proxyPassword", ""),
        help="代理密码（可选；不会打印到日志）",
    )
    p.add_argument(
        "--proxy-insecure",
        action="store_true",
        default=str(LOCAL_CONFIG.get("proxyInsecure", "")).strip().lower() in {"1", "true", "yes", "on"},
        help="允许代理返回自签 HTTPS 证书，仅用于会劫持 TLS 的代理",
    )
    p.add_argument(
        "--nav-timeout", type=int, default=_env_int(60, os.environ.get("QR_NAV_TIMEOUT")),
        help="页面跳转超时秒数（默认 60，DDG 慢可调到 120）",
    )
    p.add_argument(
        "--count", type=int, default=_env_int(1, LOCAL_CONFIG.get("count")),
        help="批量注册的账号数（默认 1）。manual 邮箱来源不支持 count > 1",
    )
    p.add_argument(
        "--cooldown", type=int, default=_env_int(30, LOCAL_CONFIG.get("cooldown")),
        help="批量注册时每个账号之间的冷却秒数（默认 30，避免 OpenAI 风控）",
    )
    p.add_argument(
        "--stop-on-error", action="store_true", default=_saved_flag("stopOnError"),
        help="批量模式下任一账号失败就停止（默认会跳过失败账号继续下一个）",
    )
    p.add_argument(
        "--no-codex-oauth", action="store_true",
        help="跳过 codex OAuth 流（默认已跳过；保留此参数用于兼容旧命令）",
    )
    p.add_argument(
        "--codex-oauth", action="store_true", default=_saved_flag("runCodexOauth"),
        help="注册成功后再跑一次 OAuth 拿完整 codex 凭据，包含 refresh_token 用于 SUB2API",
    )
    p.add_argument("--relogin-marked", action="store_true", help="重登 MySQL 中标记为需重登的账号")
    p.add_argument("--relogin-id", type=int, action="append", default=[], help="按 MySQL 账号 ID 重登，可重复传入")
    p.add_argument("--relogin-limit", type=int, default=0, help="最多重登多少个账号（0=不限制）")
    p.add_argument(
        "--relogin-timeout",
        type=int,
        default=_env_int(180, os.environ.get("QR_RELOGIN_TIMEOUT")),
        help="单个账号重登总超时秒数（默认 180）",
    )
    p.add_argument(
        "--push-id", type=int, action="append", default=[],
        help="codex-push 模式：选定要自动登录并推送到 SUB2API 的 MySQL 账号 ID，可重复传入",
    )
    p.add_argument(
        "--ban-id", type=int, action="append", default=[],
        help="ban-scan 模式：只扫描这些 MySQL 账号 ID（默认扫描全部账号）",
    )
    p.add_argument(
        "--ban-since-days", type=int,
        default=_env_int(30, os.environ.get("QR_BAN_SINCE_DAYS")),
        help="ban-scan 模式：只看最近多少天内的邮件（默认 30 天）",
    )
    p.add_argument(
        "--ban-dry-run", action="store_true",
        help="ban-scan 模式：只报告匹配到的封禁通知，不写 MySQL",
    )
    p.add_argument("--s2-base-url", default=LOCAL_CONFIG.get("s2BaseUrl", ""), help="SUB2API base URL")
    p.add_argument(
        "--s2-admin-api-key", default=LOCAL_CONFIG.get("s2AdminApiKey", ""),
        help="SUB2API 管理员 API Key（请求头 x-api-key，推荐）",
    )
    p.add_argument(
        "--s2-admin-email", default=LOCAL_CONFIG.get("s2AdminEmail", ""),
        help="SUB2API 管理员邮箱（仅在未配置 API Key 时作为 JWT 兜底）",
    )
    p.add_argument(
        "--s2-admin-password", default=LOCAL_CONFIG.get("s2AdminPassword", ""),
        help="SUB2API 管理员密码（仅在未配置 API Key 时作为 JWT 兜底）",
    )
    p.add_argument("--s2-group-name", default=LOCAL_CONFIG.get("s2GroupName") or "codex", help="SUB2API 分组名（默认 codex）")
    p.add_argument(
        "--s2-concurrency", type=int,
        default=_env_int(5, LOCAL_CONFIG.get("s2Concurrency")),
        help="SUB2API 账号并发（默认 5）",
    )
    p.add_argument(
        "--s2-priority", type=int,
        default=_env_int(1, LOCAL_CONFIG.get("s2Priority")),
        help="SUB2API 账号优先级（默认 1）",
    )
    p.add_argument(
        "--s2-rate-multiplier", type=int,
        default=_env_int(1, LOCAL_CONFIG.get("s2RateMultiplier")),
        help="SUB2API 计费倍率（默认 1）",
    )
    p.add_argument(
        "--s2-privacy-mode", default=LOCAL_CONFIG.get("s2PrivacyMode") or "training_off",
        help="SUB2API 隐私模式（默认 training_off）",
    )
    p.add_argument("--s2-dry-run", action="store_true", help="只解析 SUB2API 分组与 payload，不真正创建账号")
    p.add_argument(
        "--5sim-api-key", default=LOCAL_CONFIG.get("fiveSimApiKey", ""),
        help="5sim.net API key（Bearer token），用于 codex-oauth 的 add-phone 自动接码",
    )
    p.add_argument(
        "--5sim-country", default=LOCAL_CONFIG.get("fiveSimCountry", "any") or "any",
        help="5sim 接码国家（默认 any）",
    )
    p.add_argument(
        "--5sim-operator", default=LOCAL_CONFIG.get("fiveSimOperator", "any") or "any",
        help="5sim 接码运营商（默认 any）",
    )
    p.add_argument(
        "--5sim-product", default=LOCAL_CONFIG.get("fiveSimProduct", "openai") or "openai",
        help="5sim 服务名（默认 openai）",
    )
    p.add_argument(
        "--5sim-max-price", type=float, default=_env_float(None, LOCAL_CONFIG.get("fiveSimMaxPrice")),
        help="5sim 最高价格限制（可选）",
    )
    p.add_argument(
        "--5sim-candidate-limit", type=int, default=_env_int(8, LOCAL_CONFIG.get("fiveSimCandidateLimit")),
        help="5sim 默认筛选无号后，自动尝试多少个候选 country/operator 组合（默认 8）",
    )
    p.add_argument(
        "--5sim-acquire-priority", choices=("rate", "price"),
        default=LOCAL_CONFIG.get("fiveSimAcquirePriority") or "rate",
        help="5sim 候选号码按接码率或价格排序",
    )
    p.add_argument("--ac-check", action="store_true", help="注册/重登成功后检查当前账号 AC token")
    p.add_argument("--ac-check-batch", action="store_true", help="批量检查 MySQL 中所有账号的 token")
    p.add_argument(
        "--ac-check-base-url",
        default=LOCAL_CONFIG.get("acCheckerBaseUrl") or ac_checker.DEFAULT_BASE_URL,
        help="AC Checker base URL",
    )
    p.add_argument(
        "--ac-check-promo-id",
        default=LOCAL_CONFIG.get("acCheckerPromoId") or ac_checker.DEFAULT_PROMO_ID,
        help="AC Checker promoId",
    )
    args = p.parse_args(argv)
    # Explicit flags must beat saved settings, so detect what was actually typed
    # rather than comparing values that the saved defaults already filled in.
    typed_full_url = _argv_has(argv, "--proxy")
    typed_parts = any(
        _argv_has(argv, name)
        for name in ("--proxy-host", "--proxy-port", "--proxy-user", "--proxy-pass", "--proxy-scheme")
    )
    composed = build_proxy_url(
        host=args.proxy_host,
        port=args.proxy_port,
        user=args.proxy_user,
        password=args.proxy_pass,
        scheme=args.proxy_scheme or "http",
    )
    if typed_parts:
        args.proxy = composed or ""  # the separate fields always win (documented)
    elif typed_full_url:
        pass                         # the typed URL is already in args.proxy
    elif composed:
        args.proxy = composed        # saved fields are the saved proxy

    # The proxy is on by default (saved QR_PROXY_ENABLED, default on). Only an
    # explicit --no-proxy or a saved "off" disables it; any typed proxy flag wins.
    # Stored booleans are "1" (on) or "" (off), so an empty value means off.
    saved_off = str(LOCAL_CONFIG.get("proxyEnabled", "1") or "").strip().lower() in {"", "0", "false", "no", "off"}
    typed_proxy = typed_full_url or typed_parts
    args.proxy_enabled = not args.proxy_disabled and (typed_proxy or not saved_off)
    if not args.proxy_enabled:
        args.proxy = ""
    return args


async def _input_async(prompt: str) -> str:
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return await loop.run_in_executor(pool, input, prompt)


def _mhjc_imap_config(args: argparse.Namespace) -> "mhjc.MhjcImapConfig":
    """Resolve the MHJC mailbox credentials created for this run (never logged)."""
    address = str(getattr(args, "mhjc_email", "") or "").strip()
    mailbox_password = str(getattr(args, "mhjc_password", "") or "")
    if not address or not mailbox_password:
        raise SystemExit(
            "code-source=mhjc 需要先由 email-source=mhjc 创建邮箱，"
            "或该账号记录中缺少邮箱密码（旧账号请改用 manual 收码）"
        )
    return mhjc.MhjcImapConfig(
        email=address,
        password=mailbox_password,
        host=args.mhjc_imap_host,
        port=args.mhjc_imap_port,
    )


def make_code_fetcher(args: argparse.Namespace, *, since_ts: float):
    if args.code_source == "mhjc":
        config = _mhjc_imap_config(args)

        async def fetch_mhjc():
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor(max_workers=1) as pool:
                return await loop.run_in_executor(
                    pool,
                    lambda: mhjc.fetch_mhjc_code(
                        config,
                        max_attempts=args.mhjc_max_attempts,
                        interval_seconds=args.mhjc_interval,
                        since_ts=since_ts,
                    ),
                )
        return fetch_mhjc

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
    account_id: int | None = None
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


def _load_used_account_emails(out_dir: Path | None = None) -> set[str]:
    del out_dir  # Kept only for existing iCloud call compatibility; account state is MySQL-owned.
    return {
        email
        for account in AccountStore().list_accounts()
        if (email := str(account.get("email") or "").strip().lower())
    }


def _normalize_icloud_fetch_mode(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    return "reuse-existing" if normalized == "reuse-existing" else "always-new"


def _load_relogin_targets(args: argparse.Namespace, out_dir: Path | None = None) -> list[dict]:
    del out_dir  # Selection is by canonical database ID, not an output-directory path.
    accounts = AccountStore().list_accounts()
    by_id = {int(account["id"]): account for account in accounts}
    targets: list[dict] = []
    seen: set[int] = set()
    for value in getattr(args, "relogin_id", []) or []:
        account_id = int(value)
        account = by_id.get(account_id)
        if account is None:
            raise SystemExit(f"MySQL 中不存在账号 ID={account_id}")
        if account_id not in seen:
            targets.append(account)
            seen.add(account_id)
    if getattr(args, "relogin_marked", False):
        for account in accounts:
            account_id = int(account["id"])
            if account.get("reloginRequired") and account_id not in seen:
                targets.append(account)
                seen.add(account_id)
    limit = int(getattr(args, "relogin_limit", 0) or 0)
    if limit > 0:
        targets = targets[:limit]
    return targets


def _load_codex_push_targets(args: argparse.Namespace) -> list[dict]:
    """Selection is always explicit: only the IDs the operator ticked are processed."""
    requested = [int(value) for value in getattr(args, "push_id", []) or []]
    if not requested:
        raise SystemExit("--mode=codex-push 需要至少一个 --push-id <MySQL 账号 ID>")
    by_id = {int(account["id"]): account for account in AccountStore().list_accounts()}
    targets: list[dict] = []
    seen: set[int] = set()
    for account_id in requested:
        account = by_id.get(account_id)
        if account is None:
            raise SystemExit(f"MySQL 中不存在账号 ID={account_id}")
        if account_id not in seen:
            targets.append(account)
            seen.add(account_id)
    return targets


def _build_push_target(args: argparse.Namespace) -> "sub2api.Sub2ApiTarget":
    base_url = str(getattr(args, "s2_base_url", "") or "").strip()
    api_key = str(getattr(args, "s2_admin_api_key", "") or "").strip()
    email = str(getattr(args, "s2_admin_email", "") or "").strip()
    password = str(getattr(args, "s2_admin_password", "") or "")
    if not base_url:
        raise SystemExit("codex-push 缺少 SUB2API base URL（请在设置页填写并保存到 .env）")
    if not api_key and not (email and password):
        raise SystemExit(
            "codex-push 缺少 SUB2API 管理员 API Key（x-api-key）；"
            "该部署若未启用 API Key，可改为同时提供管理员邮箱和密码走 JWT 兜底"
        )
    return sub2api.Sub2ApiTarget(
        base_url=base_url,
        admin_api_key=api_key,
        email=email,
        password=password,
        group_name=str(getattr(args, "s2_group_name", "") or "codex"),
        concurrency=max(1, int(getattr(args, "s2_concurrency", 5) or 5)),
        priority=max(0, int(getattr(args, "s2_priority", 1) or 1)),
        rate_multiplier=max(1, int(getattr(args, "s2_rate_multiplier", 1) or 1)),
        privacy_mode=str(getattr(args, "s2_privacy_mode", "") or "training_off"),
        proxy=args.proxy or None,
        proxy_insecure=bool(args.proxy_insecure),
    )


def _build_5sim_phone_verifier(args: argparse.Namespace, *, account_id: int | None = None, account_store=None):
    """如果配置了 5sim API key，返回绑定当前 MySQL 账号的 phone_verifier。"""
    api_key = getattr(args, "5sim_api_key", None) or ""
    if not api_key:
        return None
    from core.num5sim import ActivationPool
    pool = ActivationPool.load()
    verifier = codex_oauth_module.create_5sim_phone_verifier(
        api_key=api_key,
        country=getattr(args, "5sim_country", "any") or "any",
        operator=getattr(args, "5sim_operator", "any") or "any",
        product=getattr(args, "5sim_product", "openai") or "openai",
        max_price=getattr(args, "5sim_max_price", None),
        candidate_limit=max(1, int(getattr(args, "5sim_candidate_limit", 8) or 8)),
        acquire_priority=getattr(args, "5sim_acquire_priority", "rate") or "rate",
        reuse_pool=pool,
        account_id=account_id,
        account_store=account_store,
        proxy=args.proxy or None,
        proxy_insecure=args.proxy_insecure,
    )
    return verifier


def _ac_proxy_insecure(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "proxy_insecure", False))


def _run_ac_check_for_account(
    account_data: dict,
    *,
    args: argparse.Namespace,
) -> dict[str, object]:
    token_info = ac_checker.extract_account_token(account_data)
    result = ac_checker.check_token(
        token_info["token"],
        base_url=args.ac_check_base_url,
        promo_id=args.ac_check_promo_id,
        proxy=args.proxy or None,
        proxy_insecure=_ac_proxy_insecure(args),
    )
    return {
        **result,
        "checkedAt": _utc_now(),
        "baseUrl": ac_checker.normalize_base_url(args.ac_check_base_url),
        "promoId": ac_checker.resolve_promo_id(args.ac_check_promo_id),
        "accountEmail": token_info.get("accountEmail") or account_data.get("email") or result.get("email") or "",
        "tokenSource": token_info.get("tokenSource") or result.get("tokenSource") or "",
    }


_PAYLOAD_EXCLUDED_KEYS = frozenset({
    "id", "password", "localValidity", "passwordStored", "createdAt", "updatedAt",
})


def _account_payload(account: Mapping[str, Any]) -> dict[str, Any]:
    """Strip repository-owned/derived fields and stale relogin markers from a stored record."""
    return {
        key: value
        for key, value in account.items()
        if key not in _PAYLOAD_EXCLUDED_KEYS and not key.startswith("relogin")
    }


def _store_ac_check_result(
    store: AccountStore,
    account: Mapping[str, Any],
    result: dict[str, object],
) -> dict[str, Any]:
    payload = _account_payload(account)
    payload["acCheck"] = result
    return store.save_account(str(account.get("email") or ""), str(account.get("password") or ""), payload)


def _batch_check_accounts(args: argparse.Namespace) -> dict[str, object]:
    store = AccountStore()
    rows = []
    skipped = []
    for account in store.list_accounts(include_password=True):
        email = str(account.get("email") or "")
        try:
            token_info = ac_checker.extract_account_token(account)
        except Exception as e:  # noqa: BLE001
            skipped.append({"email": email, "reason": str(e)})
            continue
        rows.append({
            "account": account,
            "token": token_info["token"],
            "tokenSource": token_info.get("tokenSource") or "",
            "accountEmail": token_info.get("accountEmail") or email,
        })

    if not rows:
        return {"count": 0, "promo_id": ac_checker.resolve_promo_id(args.ac_check_promo_id), "results": [], "skipped": skipped}

    response = ac_checker.check_tokens(
        [row["token"] for row in rows],
        base_url=args.ac_check_base_url,
        promo_id=args.ac_check_promo_id,
        proxy=args.proxy or None,
        proxy_insecure=_ac_proxy_insecure(args),
    )
    normalized_results = []
    for idx, row in enumerate(rows):
        item = dict(response["results"][idx]) if idx < len(response["results"]) else {}
        item["checkedAt"] = _utc_now()
        item["baseUrl"] = ac_checker.normalize_base_url(args.ac_check_base_url)
        item["promoId"] = response.get("promo_id") or ac_checker.resolve_promo_id(args.ac_check_promo_id)
        item["tokenSource"] = row["tokenSource"]
        item["accountEmail"] = row["accountEmail"] or item.get("email") or ""
        try:
            _store_ac_check_result(store, row["account"], item)
        except Exception as e:  # noqa: BLE001
            skipped.append({"email": row["accountEmail"], "reason": f"写入 MySQL 失败：{e}"})
            continue
        normalized_results.append({"email": row["accountEmail"], **item})

    return {
        "count": len(normalized_results),
        "promo_id": response.get("promo_id") or ac_checker.resolve_promo_id(args.ac_check_promo_id),
        "results": normalized_results,
        "skipped": skipped,
    }


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
                proxy_insecure=args.proxy_insecure,
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
        elif args.email_source == "mhjc":
            created = mhjc.create_unique_mailbox(
                args.mhjc_api_key,
                base_url=args.mhjc_api_base,
                username=args.mhjc_username,
                ttl=args.mhjc_ttl,
                proxy=args.proxy or None,
                proxy_insecure=args.proxy_insecure,
            )
            email = created["email"]
            args.mhjc_email = email
            args.mhjc_password = created["password"]
            if created.get("usernameFallback"):
                print(
                    f"[{label}] MHJC 用户名 {created['usernameFallback']!r} 已被占用，"
                    f"自动改用 {created['username']}（自定义用户名只能用一次，建议留空）"
                )
            print(f"[{label}] mhjc email -> {email}（密码已加载，不打印）")
        elif args.email_source == "icloud":
            if args.no_persistent:
                raise SystemExit("--email-source=icloud 需要持久化 profile 保存 iCloud 登录态，请不要使用 --no-persistent")
            email = await fetch_icloud_hide_my_email(
                context,
                generate_new=_normalize_icloud_fetch_mode(args.icloud_fetch_mode) == "always-new",
                host_preference=args.icloud_host,
                nav_timeout_ms=nav_timeout_ms,
                login_timeout_seconds=args.icloud_login_timeout,
                used_emails=_load_used_account_emails(out_dir),
            )
            print(f"[{label}] icloud email -> {email}")
        else:
            email = args.email.strip()
            if not email:
                raise SystemExit("--email-source=manual 需要 --email")

        # 3) 决定密码
        if args.auth_mode == "password":
            password = args.password.strip() or generate_password()
        else:
            password = args.password.strip()
        print(f"[{label}] auth-mode={args.auth_mode}; credential omitted")

        # 4) 6 步流程
        await flow.step1_open(page)
        await flow.step2_signup_email(page, email, allow_manual_cloudflare=not args.headless)
        actual_password = await flow.step3_password(page, email, password, auth_mode=args.auth_mode)

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

        extras = {
            "authMode": args.auth_mode,
            "emailSource": args.email_source,
            "codeSource": args.code_source,
            "verificationCode": code,
            "name": {"first": first_name, "last": last_name},
            "birthday": birthday.iso(),
        }
        if args.email_source == "mhjc":
            mailbox_password = str(getattr(args, "mhjc_password", "") or "")
            if mailbox_password:
                extras["emailPassword"] = mailbox_password
        account_data = session.build_account_snapshot(email, session_result=session_result, extras=extras)
        store = AccountStore()
        account = store.save_account(email, actual_password, account_data)

        # Save first so SMS verification always has a durable MySQL account ID.
        # 6) 跑 codex OAuth 拿完整凭据（refresh_token + id_token）— 给 SUB2API 用
        # OAuth 阶段会再触发一次 OTP 邮件，需要新的 since_ts 避免拿到注册阶段那封旧邮件
        codex_creds = None
        if args.codex_oauth and not args.no_codex_oauth:
            try:
                oauth_since_ts = time.time()
                oauth_fetch_code = make_code_fetcher(args, since_ts=oauth_since_ts)
                phone_verifier = _build_5sim_phone_verifier(args, account_id=int(account["id"]), account_store=store)
                codex_creds = await codex_oauth_module.run_codex_oauth(
                    page,
                    account_email=email,
                    fetch_code=oauth_fetch_code,
                    phone_verifier=phone_verifier,
                    proxy=args.proxy or None,
                    proxy_insecure=args.proxy_insecure,
                    timeout=300.0,
                )
                print(f"[{label}] [codex-oauth] OK  plan={codex_creds.get('plan_type')}  "
                      f"refresh_token={'yes' if codex_creds.get('refresh_token') else 'NO'}")
            except Exception as e:  # noqa: BLE001
                print(f"[{label}] [codex-oauth] FAILED: {e}")
        else:
            print(f"[{label}] [codex-oauth] skipped（未启用 --codex-oauth）")

        if codex_creds:
            account_data["codexAuth"] = codex_creds
            account = store.save_account(email, actual_password, account_data)
        if args.ac_check:
            try:
                ac_result = _run_ac_check_for_account(account_data, args=args)
                account_data["acCheck"] = ac_result
                account = store.save_account(email, actual_password, account_data)
                print(
                    f"[{label}] [ac-check] OK eligible={ac_result.get('eligible')} "
                    f"reason={ac_result.get('reason')} source={ac_result.get('tokenSource')}"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[{label}] [ac-check] FAILED: {e}")
        print(f"[{label}] [done] stored account id={account['id']}")
        return RunResult(email=email, status="ok", account_id=int(account["id"]))
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _run_one_relogin(
    args: argparse.Namespace,
    context,
    account_id: int,
    *,
    out_dir: Path,
    label: str,
) -> RunResult:
    del out_dir  # Account state is MySQL-owned; re-login writes no account JSON.
    store = AccountStore()
    data = store.get_account(int(account_id), include_password=True)
    if not data:
        raise RuntimeError(f"MySQL 中不存在账号 ID={account_id}")
    email = str(data.get("email") or "").strip()
    if not email:
        raise RuntimeError("MySQL 账号记录缺少 email")
    password = str(data.get("password") or "")
    auth_mode = data.get("authMode") or ("password" if password else "otp")
    previous_reason = data.get("reloginReason") or ""
    previous_marked_at = data.get("reloginMarkedAt") or ""
    if str(data.get("emailSource") or "") == "mhjc":
        args.mhjc_email = email
        args.mhjc_password = str(data.get("emailPassword") or "")
    page = await context.new_page()
    since_ts = time.time()
    try:
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
                phone_verifier = _build_5sim_phone_verifier(args, account_id=int(account_id), account_store=store)
                codex_creds = await codex_oauth_module.run_codex_oauth(
                    page,
                    account_email=email,
                    fetch_code=oauth_fetch_code,
                    phone_verifier=phone_verifier,
                    proxy=args.proxy or None,
                    proxy_insecure=args.proxy_insecure,
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

        account_data = session.build_account_snapshot(email, session_result=session_result, extras=extras)
        for key, value in _account_payload(data).items():
            if key not in account_data and value is not None:
                account_data[key] = value
        saved = store.save_account(email, password, account_data)
        if args.ac_check:
            try:
                ac_result = _run_ac_check_for_account(account_data, args=args)
                account_data["acCheck"] = ac_result
                saved = store.save_account(email, password, account_data)
                print(
                    f"[{label}] [ac-check] OK eligible={ac_result.get('eligible')} "
                    f"reason={ac_result.get('reason')} source={ac_result.get('tokenSource')}"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[{label}] [ac-check] FAILED: {e}")
        print(f"[{label}] [relogin] ✓ 已更新 MySQL 账号 id={saved['id']}（{email}）")
        return RunResult(email=email, status="ok", account_id=int(saved["id"]))
    except Exception as e:  # noqa: BLE001
        err_msg = str(e) or e.__class__.__name__
        if isinstance(e, flow.AccountBannedError):
            # A deactivated account must never be re-queued for re-login; mark it
            # and keep its stored credentials as they are.
            print(f"[{label}] [relogin] ⚠ 账号已被停用，标记封禁：{email}")
            _record_login_ban(store, int(account_id), err_msg)
            raise
        try:
            failed = _account_payload(data)
            failed["reloginRequired"] = True
            failed["reloginLastAttemptAt"] = _utc_now()
            failed["reloginLastError"] = err_msg
            store.save_account(email, password, failed)
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
    context_args = {}
    proxy_bridge = None
    if args.proxy_insecure:
        context_args["ignore_https_errors"] = True
    if args.proxy:
        effective_proxy = args.proxy
        upstream_proxy = parse_upstream_proxy(args.proxy)
        if upstream_proxy and upstream_proxy.needs_auth_bridge:
            proxy_bridge = BrowserProxyBridge(upstream_proxy)
            await proxy_bridge.start()
            effective_proxy = proxy_bridge.server_url
            print(f"[proxy] browser bridge -> {effective_proxy} -> {upstream_proxy.host}:{upstream_proxy.port}")
        browser_proxy = _build_playwright_proxy(effective_proxy)
        if browser_proxy:
            browser_args["proxy"] = browser_proxy

    user_data_dir = Path(args.user_data_dir).expanduser()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    if args.no_persistent:
        browser = await pw.chromium.launch(**browser_args)
        context = await browser.new_context(**context_args)
    else:
        _clear_stale_profile_locks(user_data_dir)
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            **context_args,
            **browser_args,
        )
        browser = None
    context.set_default_navigation_timeout(nav_timeout_ms)
    context.set_default_timeout(nav_timeout_ms)
    return pw, browser, context, proxy_bridge


async def _close_browser_context(pw, browser, context, proxy_bridge=None) -> None:
    try:
        if browser is not None:
            await context.close()
            await browser.close()
        else:
            await context.close()
        if proxy_bridge is not None:
            await proxy_bridge.close()
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
    if args.email_source == "mhjc" and not args.mhjc_api_key:
        raise SystemExit("--email-source=mhjc 需要 --mhjc-api-key（或环境变量 QR_MHJC_API_KEY）")
    if args.code_source == "mhjc" and args.email_source != "mhjc" and not args.mhjc_email:
        raise SystemExit("--code-source=mhjc 需要配合 --email-source=mhjc（本 run 尚未创建 MHJC 邮箱）")
    if args.email_source == "icloud" and args.no_persistent:
        raise SystemExit("--email-source=icloud 需要持久化 profile 保存 iCloud 登录态，请不要使用 --no-persistent")
    if args.email_source == "icloud" and args.headless:
        raise SystemExit("--email-source=icloud 不支持 headless：iCloud Web 会话在无头模式下会返回登录/421，请使用有头模式完成邮箱生成")
    args.icloud_fetch_mode = _normalize_icloud_fetch_mode(args.icloud_fetch_mode)
    if args.count > 1 and args.email_source == "manual":
        raise SystemExit("--count > 1 不支持 manual 邮箱来源（每个账号需要不同邮箱）；改用 duck / duck-api")
    if args.count < 1:
        raise SystemExit("--count 必须 ≥ 1")

    nav_timeout_ms = max(10, args.nav_timeout) * 1000

    print(f"[init] count={args.count}  cooldown={args.cooldown}s")
    print(f"[init] email-source={args.email_source}  code-source={args.code_source}")
    print(f"[init] auth-mode={args.auth_mode}  out={out_dir}")
    print(f"[init] {_proxy_banner(args)}")
    if args.proxy and args.proxy_insecure:
        print("[init] proxy-insecure=on")

    pw, browser, context, proxy_bridge = await _open_browser_context(args, nav_timeout_ms)
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
        await _close_browser_context(pw, browser, context, proxy_bridge)


async def run_relogin(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    if not args.relogin_marked and not args.relogin_id:
        raise SystemExit("--mode=relogin 需要 --relogin-marked 或 --relogin-id")
    if args.code_source == "qq" and (not args.qq_user or not args.qq_pass):
        raise SystemExit("--code-source=qq 需要同时提供 --qq-user 和 --qq-pass（IMAP 授权码）")

    targets = _load_relogin_targets(args, out_dir)
    if not targets:
        print("[relogin] 没有找到需要重登的账号")
        return 0

    nav_timeout_ms = max(10, args.nav_timeout) * 1000
    print(f"[init] mode=relogin  targets={len(targets)}  cooldown={args.cooldown}s")
    print(f"[init] code-source={args.code_source}  out={out_dir}")
    print(f"[init] {_proxy_banner(args)}")
    if args.proxy and args.proxy_insecure:
        print("[init] proxy-insecure=on")

    pw, browser, context, proxy_bridge = await _open_browser_context(args, nav_timeout_ms)
    results: list[RunResult] = []
    try:
        for i, target in enumerate(targets):
            account_id = int(target["id"])
            account_email = str(target.get("email") or "")
            label = f"{i + 1}/{len(targets)}"
            print(f"\n========== [{label}] 开始重登 {account_email or f'id={account_id}'} ==========")
            try:
                result = await _run_one_relogin(args, context, account_id, out_dir=out_dir, label=label)
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
                results.append(RunResult(email=account_email, status="fail", account_id=account_id, error=err_msg))
                if args.stop_on_error:
                    raise

            if i < len(targets) - 1 and args.cooldown > 0:
                print(f"... 冷却 {args.cooldown}s 再重登下一个 ...")
                await asyncio.sleep(args.cooldown)
    finally:
        await _close_browser_context(pw, browser, context, proxy_bridge)

    ok = [r for r in results if r.status == "ok"]
    fail = [r for r in results if r.status == "fail"]
    print()
    print(f"========== 重登结束：成功 {len(ok)} / 失败 {len(fail)} / 总计 {len(results)} ==========")
    for r in ok:
        print(f"  ✓ {r.email}  ->  {r.filename}")
    for r in fail:
        print(f"  ✗ {r.email}  失败：{r.error}")
    return 0 if not fail or not args.stop_on_error else 1


def _codex_push_result_line(result: Mapping[str, Any]) -> str:
    """One machine-readable line per account so the web UI can show real status."""
    return "[codex-push-result] " + json.dumps(dict(result), ensure_ascii=False)


async def _run_one_codex_push(
    args: argparse.Namespace,
    context,
    account_id: int,
    *,
    label: str,
) -> dict[str, Any]:
    """Freshly log in one selected account, save new credentials, then push it.

    Deliberately stricter than the tolerant re-login flow: a fresh Codex OAuth
    credential is mandatory, so a failed OAuth can never fall back to the
    previously stored `codexAuth` and push stale tokens.
    """
    store = AccountStore()
    data = store.get_account(int(account_id), include_password=True)
    if not data:
        raise RuntimeError(f"MySQL 中不存在账号 ID={account_id}")
    email = str(data.get("email") or "").strip()
    if not email:
        raise RuntimeError("MySQL 账号记录缺少 email")
    password = str(data.get("password") or "")
    auth_mode = data.get("authMode") or ("password" if password else "otp")
    if str(data.get("emailSource") or "") == "mhjc":
        args.mhjc_email = email
        args.mhjc_password = str(data.get("emailPassword") or "")
    target = _build_push_target(args)

    result: dict[str, Any] = {
        "id": int(account_id),
        "email": email,
        "login": False,
        "oauth": False,
        "saved": False,
        "pushed": False,
        "dryRun": bool(getattr(args, "s2_dry_run", False)),
        "error": "",
    }
    if data.get("banned"):
        # Pushing a dead account only creates junk in SUB2API; refuse before any
        # browser work and leave the stored credentials untouched.
        result["error"] = (
            "账号已标记封禁，跳过登录与推送："
            + (str(data.get("banReason") or "").strip() or "已被 OpenAI 停用")
        )
        print(f"[{label}] [codex-push] ⚠ {result['error']}")
        print(_codex_push_result_line(result))
        return result

    page = await context.new_page()
    # Set once the fresh credentials are written, so the failure path can never
    # overwrite them with the stale pre-login record.
    saved_payload: dict[str, Any] | None = None
    try:
        if not args.no_clear_tokens:
            # A previous account's ChatGPT cookies must not leak into this one.
            await flow.clear_openai_state(context, also_storage=True)

        print(f"[{label}] [codex-push] 开始登录：{email}  auth={auth_mode}")
        fetch_code = make_code_fetcher(args, since_ts=time.time())
        await asyncio.wait_for(
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
        result["login"] = True

        oauth_fetch_code = make_code_fetcher(args, since_ts=time.time())
        phone_verifier = _build_5sim_phone_verifier(args, account_id=account_id, account_store=store)
        codex_creds = await codex_oauth_module.run_codex_oauth(
            page,
            account_email=email,
            fetch_code=oauth_fetch_code,
            phone_verifier=phone_verifier,
            proxy=args.proxy or None,
            proxy_insecure=args.proxy_insecure,
            timeout=300.0,
        )
        if not str((codex_creds or {}).get("access_token") or ""):
            raise RuntimeError("codex OAuth 未返回 access_token，拒绝使用旧凭据")
        result["oauth"] = True

        extras = {
            "authMode": auth_mode,
            "emailSource": data.get("emailSource"),
            "codeSource": args.code_source,
            "name": data.get("name"),
            "birthday": data.get("birthday"),
            "codexAuth": codex_creds,
            "reloginRequired": False,
            "reloginLastOkAt": _utc_now(),
        }
        for key in [key for key, value in extras.items() if value is None]:
            del extras[key]
        account_data = session.build_account_snapshot(email, session_result=session_result, extras=extras)
        for key, value in _account_payload(data).items():
            if key not in account_data and value is not None:
                account_data[key] = value
        # Persist the fresh credentials BEFORE the remote create: a push failure
        # must leave usable credentials behind instead of losing them.
        store.save_account(email, password, account_data)
        saved_payload = dict(account_data)
        result["saved"] = True
        print(f"[{label}] [codex-push] 新凭据已保存到 MySQL：{email}")

        def on_progress(stage: str, info: Mapping[str, Any]) -> None:
            print(f"[{label}] [sub2api] {stage}  {dict(info)}")

        loop = asyncio.get_event_loop()
        push_result = await loop.run_in_executor(
            None,
            lambda: sub2api.push_accounts(
                [account_data],
                target,
                skip_empty=False,
                require_codex=True,
                dry_run=bool(getattr(args, "s2_dry_run", False)),
                on_progress=on_progress,
            ),
        )
        if push_result.failed:
            raise RuntimeError(str(push_result.failed[0].get("error") or "SUB2API 推送失败"))
        if not push_result.pushed:
            reason = str((push_result.skipped[0].get("reason") if push_result.skipped else "") or "SUB2API 未创建账号")
            raise RuntimeError(reason)
        result["pushed"] = True
        print(f"[{label}] [codex-push] ✓ 已推送 SUB2API：{email}")
    except Exception as e:  # noqa: BLE001
        err_msg = str(e) or e.__class__.__name__
        result["error"] = err_msg
        if isinstance(e, flow.AccountBannedError):
            # Never keep pushing / re-logging a deactivated account: mark it once
            # and leave its stored credentials untouched.
            print(f"[{label}] [codex-push] ⚠ 账号已被停用，标记封禁：{email}")
            _record_login_ban(store, int(account_id), err_msg)
        else:
            try:
                # Base the diagnostic write on the currently newest record: after a
                # successful save that is the fresh payload, never the stale one.
                failed = dict(saved_payload) if saved_payload is not None else _account_payload(data)
                failed["codexPushLastAttemptAt"] = _utc_now()
                failed["codexPushLastError"] = err_msg
                if saved_payload is None and not result["login"]:
                    # Only a failed login re-marks the account; the preserved record
                    # keeps its previous session/codex credentials untouched.
                    failed["reloginRequired"] = True
                    failed["reloginLastAttemptAt"] = _utc_now()
                    failed["reloginLastError"] = err_msg
                store.save_account(email, password, failed)
            except Exception:
                pass
        print(f"[{label}] [codex-push] ✗ 失败：{err_msg}")
    finally:
        try:
            await page.close()
        except Exception:
            pass
        print(_codex_push_result_line(result))
    return result


async def run_codex_push(args: argparse.Namespace) -> int:
    targets = _load_codex_push_targets(args)
    if not targets:
        print("[codex-push] 没有选定任何账号")
        return 0
    _build_push_target(args)  # fail fast before opening a browser

    if args.code_source == "qq" and (not args.qq_user or not args.qq_pass):
        raise SystemExit("--code-source=qq 需要同时提供 --qq-user 和 --qq-pass（IMAP 授权码）")

    nav_timeout_ms = max(10, args.nav_timeout) * 1000
    print(f"[init] mode=codex-push  targets={len(targets)}  cooldown={args.cooldown}s")
    print(f"[init] code-source={args.code_source}")
    print(f"[init] {_proxy_banner(args)}")

    pw, browser, context, proxy_bridge = await _open_browser_context(args, nav_timeout_ms)
    results: list[dict[str, Any]] = []
    try:
        for i, target in enumerate(targets):
            account_id = int(target["id"])
            label = f"{i + 1}/{len(targets)}"
            print(f"\n========== [{label}] Codex 登录并推送 {target.get('email') or f'id={account_id}'} ==========")
            results.append(await _run_one_codex_push(args, context, account_id, label=label))
            if i < len(targets) - 1 and args.cooldown > 0:
                print(f"... 冷却 {args.cooldown}s 再处理下一个 ...")
                await asyncio.sleep(args.cooldown)
    finally:
        await _close_browser_context(pw, browser, context, proxy_bridge)

    ok = [r for r in results if r["pushed"]]
    fail = [r for r in results if not r["pushed"]]
    print()
    print(f"========== codex-push 结束：成功 {len(ok)} / 失败 {len(fail)} / 总计 {len(results)} ==========")
    for r in ok:
        print(f"  ✓ {r['email']}  ->  SUB2API")
    for r in fail:
        print(f"  ✗ {r['email']}  失败：{r['error']}")
    return 0


# ---------------------------------------------------------------------------
# ban-scan：扫描邮箱，把收到封禁通知的账号在 MySQL 里标记出来
# ---------------------------------------------------------------------------

def _record_login_ban(store: AccountStore, account_id: int, error: str) -> bool:
    """Mark a ban discovered while logging in (page said the account is dead)."""
    try:
        return _mark_account_banned(store, int(account_id), {
            "reason": str(error or "登录页提示账号已被停用"),
            "source": "login",
            "subject": "",
            "date": "",
            "evidence": str(error or ""),
        })
    except Exception:  # noqa: BLE001
        return False


def _load_ban_scan_targets(args: argparse.Namespace) -> list[dict]:
    accounts = AccountStore().list_accounts()
    wanted = [int(value) for value in getattr(args, "ban_id", []) or []]
    if not wanted:
        return accounts
    by_id = {int(account["id"]): account for account in accounts}
    targets: list[dict] = []
    seen: set[int] = set()
    for account_id in wanted:
        account = by_id.get(account_id)
        if account is None:
            raise SystemExit(f"MySQL 中不存在账号 ID={account_id}")
        if account_id not in seen:
            targets.append(account)
            seen.add(account_id)
    return targets


def _collect_ban_records(args: argparse.Namespace, targets: list[dict]) -> tuple[list[dict], list[str]]:
    """Read every mailbox that can hold a ban notice for the given accounts.

    duck / iCloud aliases all forward into the shared QQ mailbox, so one QQ pass
    covers them; each MHJC account has its own mailbox and is scanned with the
    password stored on that MySQL row.
    """
    since_ts = time.time() - max(1, int(getattr(args, "ban_since_days", 30) or 30)) * 86400
    records: list[dict] = []
    errors: list[str] = []

    mhjc_targets = [a for a in targets if str(a.get("emailSource") or "") == "mhjc"]
    non_mhjc = [a for a in targets if str(a.get("emailSource") or "") != "mhjc"]

    if non_mhjc and args.qq_user and args.qq_pass:
        config = QQImapConfig(
            user=args.qq_user,
            password=args.qq_pass,
            host=args.qq_host,
            port=args.qq_port,
        )
        try:
            records += qq_imap.scan_qq_messages(config, since_ts=since_ts)
            print(f"[ban-scan] QQ 邮箱已读取（覆盖 {len(non_mhjc)} 个转发账号）")
        except Exception as e:  # noqa: BLE001
            message = f"QQ 邮箱扫描失败：{e}"
            errors.append(message)
            print(f"[ban-scan] {message}")
    elif non_mhjc:
        print("[ban-scan] 未配置 QQ IMAP 凭据，跳过共享收件箱（duck/iCloud 转发账号无法扫描）")

    for account in mhjc_targets:
        email = str(account.get("email") or "")
        # `list_accounts()` already carries the stored mailbox password.
        password = str(account.get("emailPassword") or "")
        if not password:
            errors.append(f"{email} 缺少邮箱密码，无法扫描其 MHJC 收件箱")
            continue
        try:
            config = mhjc.MhjcImapConfig(email=email, password=password)
            records += mhjc.scan_mhjc_messages(config, since_ts=since_ts)
            print(f"[ban-scan] MHJC 邮箱已读取：{email}")
        except Exception as e:  # noqa: BLE001
            message = f"{email} 邮箱扫描失败：{e}"
            errors.append(message)
            print(f"[ban-scan] {message}")
    return records, errors


def _mark_account_banned(store: AccountStore, account_id: int, evidence: Mapping[str, Any]) -> bool:
    """Write the ban mark onto the same MySQL row, preserving every credential."""
    data = store.get_account(int(account_id), include_password=True)
    if not data:
        return False
    email = str(data.get("email") or "").strip()
    if not email:
        return False
    payload = _account_payload(data)
    payload["banned"] = True
    payload["banReason"] = str(evidence.get("reason") or "")
    payload["banSubject"] = str(evidence.get("subject") or "")
    payload["banDate"] = str(evidence.get("date") or "")
    payload["banEvidence"] = str(evidence.get("evidence") or "")
    payload["banSource"] = str(evidence.get("source") or "email")
    payload["banDetectedAt"] = _utc_now()
    # A banned account no longer needs a re-login: keep the flag noise down but
    # never touch the stored session/codex credentials.
    payload["reloginRequired"] = False
    store.save_account(email, str(data.get("password") or ""), payload)
    return True


async def run_ban_scan(args: argparse.Namespace) -> int:
    targets = _load_ban_scan_targets(args)
    if not targets:
        print("[ban-scan] MySQL 中还没有账号")
        return 0

    print(f"[init] mode=ban-scan  accounts={len(targets)}  since={max(1, int(args.ban_since_days))}d")
    loop = asyncio.get_event_loop()
    records, errors = await loop.run_in_executor(None, lambda: _collect_ban_records(args, targets))
    print(f"[ban-scan] 共读取 {len(records)} 封邮件，开始匹配停用通知")

    found = ban_check.find_banned_accounts(records, targets)
    dry_run = bool(getattr(args, "ban_dry_run", False))
    store = AccountStore()
    marked: list[dict] = []
    for account_id, evidence in sorted(found.items()):
        email = next((str(a.get("email") or "") for a in targets if int(a["id"]) == int(account_id)), "")
        row = {
            "id": int(account_id),
            "email": email,
            "banned": True,
            "reason": evidence.get("reason") or "",
            "subject": evidence.get("subject") or "",
            "date": evidence.get("date") or "",
            "dryRun": dry_run,
        }
        if not dry_run:
            try:
                ok = _mark_account_banned(store, account_id, evidence)
            except Exception as e:  # noqa: BLE001
                print(f"[ban-scan] ✗ 标记失败 id={account_id}：{e}")
                continue
            if not ok:
                continue
        marked.append(row)
        prefix = "（dry-run，未写库）" if dry_run else "已标记封禁"
        print(f"[ban-scan] ⚠ {prefix}：{email}  原因：{row['reason']}")
        print("[ban-scan-result] " + json.dumps(row, ensure_ascii=False))

    for message in errors:
        print(f"[ban-scan] 注意：{message}")
    print()
    verb = "命中" if dry_run else "新标记"
    print(f"========== ban-scan 结束：扫描 {len(targets)} 个账号 / 本次{verb} {len(marked)} 个 / 邮件错误 {len(errors)} 条 ==========")
    if not marked:
        print("  未发现新的封禁账号（已标记的账号不会被自动清除）")
    return 0


async def run(args: argparse.Namespace) -> int:
    if args.ac_check_batch:
        summary = await asyncio.get_event_loop().run_in_executor(None, lambda: _batch_check_accounts(args))
        print(
            f"[ac-check-batch] count={summary.get('count', 0)} "
            f"skipped={len(summary.get('skipped', []))} promo={summary.get('promo_id')}"
        )
        for item in summary.get("results", []):
            print(
                f"[ac-check-batch] {item.get('email')} eligible={item.get('eligible')} "
                f"reason={item.get('reason')} source={item.get('tokenSource')}"
            )
        for item in summary.get("skipped", []):
            print(f"[ac-check-batch] skip {item.get('email')}: {item.get('reason')}")
        return 0
    if args.mode == "relogin":
        return await run_relogin(args)
    if args.mode == "codex-push":
        return await run_codex_push(args)
    if args.mode == "ban-scan":
        return await run_ban_scan(args)
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
