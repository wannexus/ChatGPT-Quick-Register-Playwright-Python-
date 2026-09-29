"""AntBrowser（ant-chrome）本地指纹浏览器接入。

每个账号一个实例（一份指纹），启动后用 CDP 交给 Playwright 接管 —— 指纹由
AntBrowser 的内核参数决定，Playwright 只做自动化，不会覆盖指纹。

本地 Launch API（默认 127.0.0.1:19876，AntBrowser 内「设置 → 启动服务」可改端口）：
    GET    /api/health
    GET    /api/profiles
    POST   /api/profiles                {"profile": {...}}
    GET    /api/profiles/{id}
    DELETE /api/profiles/{id}
    POST   /api/launch                  {"selector": {...}, "skipDefaultStartUrls": true, "proxyConfig": ""}
    POST   /api/runtime/session         {"selector": {"profileId": "...", "matchMode": "unique"}}
    POST   /api/runtime/active
    POST   /api/profiles/{id}/stop

约定：所有请求都直连本机，绝不过代理（显式清空 ProxyHandler）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from core import fingerprint as fingerprint_module
from core.fingerprint import FingerprintIdentity

DEFAULT_API_BASE = "http://127.0.0.1:19876"
API_KEY_HEADER = "X-Ant-Api-Key"
DEFAULT_TIMEOUT = 15.0
LAUNCH_TIMEOUT = 60.0
LAUNCH_READY_TIMEOUT = 45.0


class AntBrowserError(RuntimeError):
    """AntBrowser 接入失败的基类。"""


class AntBrowserUnavailable(AntBrowserError):
    """本地 Launch API 连不上（应用没开 / 端口不对）。"""


class AntBrowserApiError(AntBrowserError):
    """Launch API 返回了错误。"""


@dataclass
class AntBrowserSession:
    """一次已启动的指纹实例。"""

    profile_id: str
    profile_name: str
    cdp_url: str
    debug_port: int = 0
    pid: int = 0
    launch_code: str = ""
    created_profile: bool = False
    identity: FingerprintIdentity | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        origin = "新建实例" if self.created_profile else "复用实例"
        return (f"{origin} {self.profile_name} profile={self.profile_id} "
                f"cdp={self.cdp_url} pid={self.pid}")


def proxy_config_for_ant(proxy_value: str) -> str:
    """把代理 URL 规范成 AntBrowser 的 proxyConfig（凭据要 URL 编码）。

    手工切分而不是 urlsplit：代理密码里出现 `+ / @ :` 等未编码字符时，urlsplit 会把
    密码当端口直接抛 ValueError。这里先 unquote 再 quote，对「已编码」和「未编码」
    两种写法都幂等，不会把 %2B 二次编码成 %252B。
    """
    raw = str(proxy_value or "").strip()
    if not raw:
        return ""
    scheme, sep, rest = raw.partition("://")
    if not sep or not rest:
        return raw
    auth, at, host_port = rest.rpartition("@")
    if not at:
        auth, host_port = "", rest
    if not host_port:
        return raw

    def _clean(part: str) -> str:
        return urllib.parse.quote(urllib.parse.unquote(part), safe="")

    credentials = ""
    if auth:
        user, colon, password = auth.partition(":")
        credentials = _clean(user)
        if colon:
            credentials += ":" + _clean(password)
        credentials += "@"
    return f"{scheme}://{credentials}{host_port}"


# 指纹探测/比对逻辑现在住在 core/fingerprint.py（由指纹层共用）；这里重导出保持兼容。
FINGERPRINT_PROBE_JS = fingerprint_module.FINGERPRINT_PROBE_JS
fingerprint_mismatches = fingerprint_module.fingerprint_mismatches


FINGERPRINT_CORE_HINT = (
    "常见原因：AntBrowser 当前实例用的内核是普通 Chrome（内核管理里只有「谷歌」），"
    "Chrome 会静默忽略 --fingerprint*/--timezone。请在 AntBrowser「内核管理」里添加 "
    "fingerprint-chromium 内核（README 推荐 adryfish/fingerprint-chromium 的 Releases），"
    "再重新跑本流程。"
)


class AntBrowserClient:
    """AntBrowser 本地 Launch API 客户端。"""

    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        self.base_url = (str(base_url or "").strip() or DEFAULT_API_BASE).rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout = float(timeout)
        self._opener = opener or self._default_opener

    # ---------------- HTTP ----------------
    @staticmethod
    def _default_opener(request: urllib.request.Request, timeout: float):
        # 本机 API：显式禁用代理，避免被 HTTP_PROXY/系统代理劫持
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)

    def _request(self, method: str, path: str, payload: dict | None = None,
                 timeout: float | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers[API_KEY_HEADER] = self.api_key
        request = urllib.request.Request(url, method=method, headers=headers, data=body)
        try:
            with self._opener(request, timeout or self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            raise AntBrowserApiError(f"AntBrowser API HTTP {exc.code} {path}：{detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AntBrowserUnavailable(
                f"连不上 AntBrowser Launch API {self.base_url}（{reason}）。"
                "请先打开 AntBrowser，并在「设置 → 启动服务」确认端口与地址；"
                "或改用 --fingerprint-browser off 关闭指纹浏览器接入。"
            ) from exc

        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AntBrowserApiError(f"AntBrowser API 返回的不是 JSON（{path}）：{text[:200]}") from exc
        if not isinstance(parsed, dict):
            raise AntBrowserApiError(f"AntBrowser API 返回结构异常（{path}）")
        return parsed

    def _expect_ok(self, payload: dict[str, Any], action: str) -> dict[str, Any]:
        if payload.get("ok") is False:
            raise AntBrowserApiError(f"{action}失败：{payload.get('error') or '未知错误'}")
        return payload

    # ---------------- 基础接口 ----------------
    def health(self) -> bool:
        try:
            return bool(self._request("GET", "/api/health", timeout=min(self.timeout, 5.0)).get("ok"))
        except AntBrowserError:
            return False

    def list_profiles(self) -> list[dict[str, Any]]:
        payload = self._expect_ok(self._request("GET", "/api/profiles"), "读取实例列表")
        items = payload.get("items")
        return [item for item in items or [] if isinstance(item, dict)]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        try:
            payload = self._request("GET", f"/api/profiles/{urllib.parse.quote(profile_id, safe='')}")
        except AntBrowserApiError:
            return None
        profile = payload.get("profile")
        return profile if isinstance(profile, dict) else None

    def default_core_id(self) -> str:
        for profile in self.list_profiles():
            core_id = str(profile.get("coreId") or "").strip()
            if core_id:
                return core_id
        return ""

    def create_profile(
        self,
        name: str,
        *,
        fingerprint_args: list[str],
        launch_args: list[str] | None = None,
        proxy_config: str = "",
        tags: list[str] | None = None,
        core_id: str = "",
    ) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "profileName": str(name or "qr-profile")[:60],
            "fingerprintArgs": list(fingerprint_args or []),
            "launchArgs": list(launch_args or []),
            "tags": list(tags or []),
        }
        if core_id:
            profile["coreId"] = core_id
        if proxy_config:
            profile["proxyConfig"] = proxy_config
        payload = self._expect_ok(
            self._request("POST", "/api/profiles", {"profile": profile}), "创建指纹实例"
        )
        created = payload.get("profile")
        if not isinstance(created, dict) or not created.get("profileId"):
            raise AntBrowserApiError("创建指纹实例失败：响应缺少 profileId")
        return created

    def launch(self, *, profile_id: str = "", launch_code: str = "",
               proxy_config: str = "", timeout: float = LAUNCH_TIMEOUT) -> dict[str, Any]:
        selector: dict[str, Any] = {"matchMode": "unique"}
        if profile_id:
            selector["profileId"] = profile_id
        elif launch_code:
            selector["code"] = str(launch_code).strip().upper()
        else:
            raise AntBrowserApiError("启动指纹实例需要 profileId 或 launchCode")
        body: dict[str, Any] = {"selector": selector, "skipDefaultStartUrls": True}
        if proxy_config:
            body["proxyConfig"] = proxy_config
        return self._expect_ok(self._request("POST", "/api/launch", body, timeout=timeout), "启动指纹实例")

    def session(self, profile_id: str) -> dict[str, Any]:
        body = {"selector": {"profileId": profile_id, "matchMode": "unique"}}
        return self._expect_ok(self._request("POST", "/api/runtime/session", body), "读取实例会话")

    def active(self) -> dict[str, Any]:
        return self._expect_ok(self._request("GET", "/api/runtime/active"), "读取活动实例")

    def stop(self, profile_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
        path = f"/api/profiles/{urllib.parse.quote(profile_id, safe='')}/stop"
        return self._expect_ok(self._request("POST", path, {}, timeout=timeout), "停止指纹实例")

    def delete_profile(self, profile_id: str) -> bool:
        path = f"/api/profiles/{urllib.parse.quote(profile_id, safe='')}"
        payload = self._expect_ok(self._request("DELETE", path), "删除指纹实例")
        return bool(payload.get("deleted"))

    # ---------------- 高层流程 ----------------
    @staticmethod
    def _cdp_url(session_payload: dict[str, Any]) -> str:
        direct = str(session_payload.get("directDebugUrl") or "").strip()
        if direct:
            return direct
        return str(session_payload.get("cdpUrl") or "").strip()

    def start_session(
        self,
        *,
        name: str,
        identity: FingerprintIdentity,
        proxy_config: str = "",
        reuse_profile_id: str = "",
        core_id: str = "",
        ready_timeout: float = LAUNCH_READY_TIMEOUT,
    ) -> AntBrowserSession:
        """复用/新建一个指纹实例并启动，返回可直接 CDP 接管的会话。"""
        created = False
        profile_id = str(reuse_profile_id or "").strip()
        if profile_id and self.get_profile(profile_id) is None:
            profile_id = ""
        if not profile_id:
            created_profile = self.create_profile(
                name,
                fingerprint_args=identity.args,
                launch_args=["--disable-sync", "--no-first-run"],
                proxy_config=proxy_config,
                tags=["qr-auto"],
                core_id=core_id or self.default_core_id(),
            )
            profile_id = str(created_profile["profileId"])
            created = True

        try:
            result = self.launch(profile_id=profile_id, proxy_config=proxy_config)
        except AntBrowserApiError:
            # 可能已经处于运行状态：直接取会话
            result = self.session(profile_id)

        cdp_url = self._cdp_url(result)
        deadline = time.time() + max(1.0, ready_timeout)
        while (not result.get("debugReady") or not cdp_url) and time.time() < deadline:
            time.sleep(0.5)
            try:
                result = self.session(profile_id)
            except AntBrowserError:
                break
            cdp_url = self._cdp_url(result)
        if not cdp_url:
            raise AntBrowserApiError(
                f"指纹实例 {profile_id} 启动后没有拿到 CDP 地址"
                f"（runtimeWarning={result.get('runtimeWarning') or result.get('lastError') or '无'}）"
            )
        session = AntBrowserSession(
            profile_id=profile_id,
            profile_name=str(result.get("profileName") or name),
            cdp_url=cdp_url,
            debug_port=int(result.get("debugPort") or 0),
            pid=int(result.get("pid") or 0),
            launch_code=str(result.get("launchCode") or ""),
            created_profile=created,
            identity=identity,
            raw=result,
        )
        return session


__all__ = [
    "API_KEY_HEADER",
    "AntBrowserApiError",
    "AntBrowserClient",
    "AntBrowserError",
    "AntBrowserSession",
    "AntBrowserUnavailable",
    "DEFAULT_API_BASE",
    "FINGERPRINT_CORE_HINT",
    "FINGERPRINT_PROBE_JS",
    "fingerprint_mismatches",
    "proxy_config_for_ant",
]
