"""Test an upstream proxy: what IP does it really give you, and is it home broadband?

Two steps, both against public services:

1. Ask an "what is my IP" echo endpoint **through the proxy** to learn the exit IP.
2. Look that IP up to see whether it belongs to a hosting/datacenter network
   (机房 / IDC) or an ordinary consumer ISP (家宽 / 住宅).

The second lookup deliberately runs *directly* rather than through the proxy, so a
broken proxy cannot hide the classification result. Verdicts are advisory: they
describe public IP intelligence, not a guarantee about any particular service.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

from core.http_utils import open_url, redact_proxy_url

DEFAULT_TIMEOUT = 15.0

# Echo endpoints, tried in order until one returns a usable IP.
DEFAULT_IP_ECHO_URLS = (
    "https://api.ipify.org?format=json",
    "http://ip-api.com/json/?fields=status,query",
    "https://ipwho.is/",
)

LOOKUP_TEMPLATE = (
    "http://ip-api.com/json/{ip}"
    "?fields=status,message,country,regionName,city,isp,org,as,hosting,proxy,mobile"
)
LOOKUP_FALLBACK_TEMPLATE = "https://ipwho.is/{ip}"

# ISP / ASN names that mean "this is rented rack space, not somebody's flat".
DATACENTER_KEYWORDS = (
    "amazon", "aws", "google", "microsoft", "azure", "oracle", "alibaba", "aliyun",
    "tencent", "huawei cloud", "digitalocean", "linode", "akamai", "vultr", "hetzner",
    "ovh", "contabo", "leaseweb", "m247", "choopa", "quadranet", "colocrossing",
    "cloudflare", "fastly", "scaleway", "upcloud", "ionos", "gcore", "hostwinds",
    "datacenter", "data center", "datacentre", "hosting", "host ", "server", "vps",
    "colo", "dedicated", "idc", "cloud", "rack", "bare metal", "netcup", "racknerd",
)
MOBILE_KEYWORDS = (
    "mobile", "cellular", "wireless", "4g", "5g", "lte", "t-mobile", "vodafone",
    "telefonica", "orange s.a", "china mobile", "china unicom", "china telecom mobile",
)

LABELS = {
    "residential": "家宽 / 住宅",
    "datacenter": "机房 / IDC",
    "mobile": "移动网络",
    "unknown": "未知",
}


def _is_ip(value: Any) -> bool:
    try:
        ipaddress.ip_address(str(value).strip())
        return True
    except ValueError:
        return False


def parse_ip_echo(payload: Any) -> str | None:
    """Extract an IP from any of the echo endpoints' response shapes."""
    if payload is None:
        return None
    data: Any = payload
    if isinstance(payload, (bytes, str)):
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        text = text.strip()
        if _is_ip(text):
            return text
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None
    for key in ("ip", "query", "origin", "address"):
        candidate = data.get(key)
        if candidate and _is_ip(candidate):
            return str(candidate).strip()
    return None


def parse_lookup(payload: Any, *, source: str = "") -> dict[str, Any]:
    """Normalise ip-api.com / ipwho.is payloads into one flat shape."""
    data = payload if isinstance(payload, dict) else {}
    status = str(data.get("status") or "").lower()
    if status == "fail":
        return {"ok": False, "error": str(data.get("message") or "查询失败"), "source": source}
    if data.get("success") is False:
        return {"ok": False, "error": str(data.get("message") or "查询失败"), "source": source}

    connection = data.get("connection") if isinstance(data.get("connection"), dict) else {}
    asn_value = data.get("as") or connection.get("asn")
    asn_text = ""
    if asn_value is not None and str(asn_value).strip():
        asn_text = str(asn_value).strip()
        if not asn_text.upper().startswith("AS"):
            org = str(connection.get("org") or data.get("org") or data.get("isp") or "").strip()
            asn_text = f"AS{asn_text}" + (f" {org}" if org else "")

    asn_name = asn_text
    if asn_text.upper().startswith("AS") and " " not in asn_text:
        asn_name = asn_text

    return {
        "ok": True,
        "ip": str(data.get("query") or data.get("ip") or "").strip() or None,
        "country": data.get("country") or None,
        "region": data.get("regionName") or data.get("region") or None,
        "city": data.get("city") or None,
        "isp": (connection.get("isp") or data.get("isp") or connection.get("org") or data.get("org") or None),
        "org": data.get("org") or connection.get("org") or None,
        "asn": asn_name or None,
        "hosting": data.get("hosting"),
        "proxy": data.get("proxy"),
        "mobile": data.get("mobile"),
        "source": source,
    }


def _matches(text: str, needles: Iterable[str]) -> str | None:
    lowered = str(text or "").lower()
    for needle in needles:
        if needle in lowered:
            return needle
    return None


def classify_ip(info: dict[str, Any] | None) -> dict[str, Any]:
    """Decide whether an IP looks like consumer broadband, a datacenter or mobile.

    `hosting`/`mobile` flags from the lookup win; ISP/ASN names are the fallback.
    """
    info = info or {}
    isp = str(info.get("isp") or "")
    org = str(info.get("org") or "")
    asn = str(info.get("asn") or "")
    haystack = f"{isp} {org} {asn}"

    hosting = info.get("hosting")
    mobile = info.get("mobile")
    reasons: list[str] = []
    notes: list[str] = []

    if mobile is True:
        kind, confidence = "mobile", "high"
        reasons.append("查询结果标记为移动网络（mobile=true）")
    elif hosting is True:
        kind, confidence = "datacenter", "high"
        reasons.append("查询结果标记为机房/托管（hosting=true）")
    elif hosting is False:
        # An explicit "not hosting" answer is the strongest residential signal.
        kind, confidence = "residential", "high"
        reasons.append("查询结果明确标记为非机房（hosting=false）")
    else:
        keyword = _matches(haystack, DATACENTER_KEYWORDS)
        mobile_keyword = _matches(haystack, MOBILE_KEYWORDS)
        if keyword:
            kind, confidence = "datacenter", "medium"
            reasons.append(f"运营商/ASN 名称命中机房关键词「{keyword}」")
        elif mobile_keyword:
            kind, confidence = "mobile", "medium"
            reasons.append(f"运营商/ASN 名称命中移动网络关键词「{mobile_keyword}」")
        elif isp or asn:
            kind, confidence = "residential", "medium"
            reasons.append("运营商名称未命中机房/移动网络关键词，倾向住宅线路")
        else:
            kind, confidence = "unknown", "low"
            reasons.append("没有足够的归属信息可判断（查询可能被限流或该 IP 信息缺失）")

    if info.get("proxy") is True:
        notes.append("该 IP 被标记为已知代理/VPN，部分服务可能因此加强风控")
    if not reasons:
        reasons.append("无判定依据")

    return {
        "kind": kind,
        "label": LABELS[kind],
        "isResidential": kind == "residential",
        "confidence": confidence,
        "reasons": reasons,
        "notes": notes,
        "flagged": info.get("proxy") is True,
    }


def lookup_url(ip: str) -> str:
    return LOOKUP_TEMPLATE.format(ip=urllib.parse.quote(str(ip).strip()))


def _request_json(url: str, *, proxy: str | None, proxy_insecure: bool, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8.0", "Accept": "application/json"})
    with open_url(request, proxy=proxy, insecure=proxy_insecure, timeout=timeout) as response:
        body = response.read(65536)
    text = body.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def fetch_exit_ip(
    proxy: str,
    *,
    proxy_insecure: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    echo_urls: Iterable[str] = DEFAULT_IP_ECHO_URLS,
) -> str:
    """Return the public IP as seen *through* the proxy (empty proxy = direct)."""
    errors: list[str] = []
    for url in echo_urls:
        try:
            payload = _request_json(url, proxy=proxy or None, proxy_insecure=proxy_insecure, timeout=timeout)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{urllib.parse.urlsplit(url).netloc}: {exc}")
            continue
        ip = parse_ip_echo(payload)
        if ip:
            return ip
        errors.append(f"{urllib.parse.urlsplit(url).netloc}: 响应中没有 IP")
    raise RuntimeError("；".join(errors) or "无法获取出口 IP")


def lookup_ip(ip: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Look an IP up directly (not through the proxy) and normalise the answer."""
    try:
        payload = _request_json(lookup_url(ip), proxy=None, proxy_insecure=False, timeout=timeout)
        info = parse_lookup(payload, source="ip-api")
        if info.get("ok"):
            return info
    except (urllib.error.URLError, OSError, ValueError):
        pass

    try:
        payload = _request_json(
            LOOKUP_FALLBACK_TEMPLATE.format(ip=urllib.parse.quote(str(ip).strip())),
            proxy=None, proxy_insecure=False, timeout=timeout,
        )
        return parse_lookup(payload, source="ipwho.is")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "source": ""}


def test_proxy(
    proxy: str,
    *,
    proxy_insecure: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    compare_direct: bool = True,
) -> dict[str, Any]:
    """Test the proxy and classify its exit IP. Never raises for network failures."""
    raw = str(proxy or "").strip()
    result: dict[str, Any] = {
        "ok": False,
        "proxy": redact_proxy_url(raw),
        "exitIp": None,
        "directIp": None,
        "changedIp": None,
        "info": {},
        "verdict": classify_ip({}),
        "notes": [],
        "error": "",
    }
    if not raw:
        result["error"] = "没有配置代理（请填写代理主机/端口，或完整的代理 URL）"
        return result

    try:
        result["exitIp"] = fetch_exit_ip(raw, proxy_insecure=proxy_insecure, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
        result["error"] = f"通过代理获取出口 IP 失败：{exc}"
        return result

    if compare_direct:
        try:
            result["directIp"] = fetch_exit_ip("", timeout=timeout)
        except Exception:  # noqa: BLE001 - comparison is best effort
            result["directIp"] = None
        if result["directIp"]:
            result["changedIp"] = result["directIp"] != result["exitIp"]
            if not result["changedIp"]:
                result["notes"].append("出口 IP 与直连 IP 相同：该代理没有改变你的公网 IP（可能是透明代理或配置未生效）")

    info = lookup_ip(result["exitIp"], timeout=timeout)
    result["info"] = info
    if info.get("ok"):
        verdict = classify_ip(info)
        result["ok"] = True
        result["verdict"] = verdict
        result["notes"].extend(verdict["notes"])
    else:
        result["error"] = f"已取得出口 IP，但归属查询失败：{info.get('error') or '未知原因'}"
        result["ok"] = True  # the proxy itself works; only enrichment failed
        result["notes"].append("归属查询失败，无法判断是家宽还是机房")

    return result


__all__ = [
    "DEFAULT_IP_ECHO_URLS",
    "classify_ip",
    "fetch_exit_ip",
    "lookup_ip",
    "lookup_url",
    "parse_ip_echo",
    "parse_lookup",
    "test_proxy",
]
