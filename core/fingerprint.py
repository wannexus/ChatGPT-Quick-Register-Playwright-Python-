"""每次注册使用不同浏览器指纹（真人画像 + 随机种子）。

只负责「生成一份自洽的指纹配置」，不关心谁来执行：
- AntBrowser 路径：转成 fingerprint-chromium 命令行参数（core/ant_browser.py）
- 普通 Playwright 兜底路径：转成 new_context(...) 参数 + 少量 init script（register.py）

画像取值来自 AntBrowser 官方能力矩阵里「实测生效」的参数集合：
种子、浏览器身份（brand/platform/版本）、语言与时区、CPU 核心数、Canvas 噪声、
ClientRects 噪声、WebRTC 防泄漏；deviceMemory/色深/触点数/独立 GPU 参数在该内核上实测无效，
所以不再传递，避免出现「参数写了但运行时没生效」的假指纹。
"""

from __future__ import annotations

import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

# Chrome 主版本尽量贴近本机内核；brand-version 只影响 UA-CH 画像
DEFAULT_BRAND_VERSION = "154.0.7951.96"


@dataclass(frozen=True)
class Persona:
    """一套自洽的真人画像：地区 + 平台 + 语言 + 时区 + 分辨率。"""

    region: str
    platform: str          # windows | macos | linux
    brand: str             # Chrome | Edge | Opera | Vivaldi
    lang: str
    accept_lang: str
    timezone: str
    resolution: str        # "1920,1080"
    cores: int
    platform_version: str = ""
    brand_version: str = ""

    @property
    def screen(self) -> tuple[int, int]:
        width, _, height = self.resolution.partition(",")
        return int(width or 1920), int(height or 1080)


PERSONAS: tuple[Persona, ...] = (
    Persona("US", "windows", "Chrome", "en-US", "en-US,en", "America/New_York", "1920,1080", 8, "10.0.0"),
    Persona("US", "windows", "Edge", "en-US", "en-US,en", "America/Chicago", "1536,864", 12, "10.0.0"),
    Persona("US", "macos", "Chrome", "en-US", "en-US,en", "America/Los_Angeles", "1512,982", 10, "15.3.0"),
    Persona("CA", "windows", "Chrome", "en-CA", "en-CA,en", "America/Toronto", "1920,1080", 8, "10.0.0"),
    Persona("GB", "windows", "Edge", "en-GB", "en-GB,en", "Europe/London", "1920,1080", 8, "10.0.0"),
    Persona("GB", "macos", "Chrome", "en-GB", "en-GB,en", "Europe/London", "1440,900", 8, "14.6.0"),
    Persona("DE", "windows", "Chrome", "de-DE", "de-DE,de", "Europe/Berlin", "1920,1080", 12, "10.0.0"),
    Persona("DE", "linux", "Chrome", "de-DE", "de-DE,de", "Europe/Berlin", "1920,1080", 12, ""),
    Persona("FR", "windows", "Chrome", "fr-FR", "fr-FR,fr", "Europe/Paris", "1600,900", 8, "10.0.0"),
    Persona("NL", "windows", "Chrome", "nl-NL", "nl-NL,nl", "Europe/Amsterdam", "1920,1080", 8, "10.0.0"),
    Persona("ES", "windows", "Chrome", "es-ES", "es-ES,es", "Europe/Madrid", "1366,768", 8, "10.0.0"),
    Persona("IT", "windows", "Chrome", "it-IT", "it-IT,it", "Europe/Rome", "1920,1080", 8, "10.0.0"),
    Persona("PL", "windows", "Chrome", "pl-PL", "pl-PL,pl", "Europe/Warsaw", "1920,1080", 8, "10.0.0"),
    Persona("SE", "windows", "Chrome", "sv-SE", "sv-SE,sv", "Europe/Stockholm", "1920,1080", 8, "10.0.0"),
    Persona("JP", "macos", "Chrome", "ja-JP", "ja-JP,ja", "Asia/Tokyo", "1440,900", 8, "15.2.0"),
    Persona("JP", "windows", "Chrome", "ja-JP", "ja-JP,ja", "Asia/Tokyo", "1920,1080", 8, "10.0.0"),
    Persona("KR", "windows", "Chrome", "ko-KR", "ko-KR,ko", "Asia/Seoul", "1920,1080", 8, "10.0.0"),
    Persona("TW", "windows", "Chrome", "zh-TW", "zh-TW,zh", "Asia/Taipei", "1920,1080", 8, "10.0.0"),
    Persona("HK", "macos", "Chrome", "zh-HK", "zh-HK,zh", "Asia/Hong_Kong", "1440,900", 8, "14.5.0"),
    Persona("SG", "windows", "Chrome", "en-SG", "en-SG,en", "Asia/Singapore", "1920,1080", 8, "10.0.0"),
    Persona("AU", "windows", "Chrome", "en-AU", "en-AU,en", "Australia/Sydney", "1920,1080", 8, "10.0.0"),
    Persona("BR", "windows", "Chrome", "pt-BR", "pt-BR,pt", "America/Sao_Paulo", "1366,768", 8, "10.0.0"),
    Persona("IN", "windows", "Chrome", "en-IN", "en-IN,en", "Asia/Kolkata", "1600,900", 8, "10.0.0"),
    Persona("ID", "windows", "Chrome", "id-ID", "id-ID,id", "Asia/Jakarta", "1366,768", 8, "10.0.0"),
    Persona("VN", "windows", "Chrome", "vi-VN", "vi-VN,vi", "Asia/Ho_Chi_Minh", "1366,768", 8, "10.0.0"),
    Persona("TH", "windows", "Chrome", "th-TH", "th-TH,th", "Asia/Bangkok", "1536,864", 8, "10.0.0"),
    Persona("PH", "windows", "Chrome", "en-PH", "en-PH,en", "Asia/Manila", "1366,768", 8, "10.0.0"),
    Persona("MY", "windows", "Chrome", "en-MY", "en-MY,en", "Asia/Kuala_Lumpur", "1920,1080", 8, "10.0.0"),
)

# 5sim 国家 slug → 画像地区，让浏览器时区/语言与号码归属地尽量一致
COUNTRY_REGION = {
    "usa": "US", "canada": "CA", "england": "GB", "uk": "GB", "germany": "DE",
    "france": "FR", "netherlands": "NL", "spain": "ES", "italy": "IT",
    "poland": "PL", "sweden": "SE", "japan": "JP", "southkorea": "KR",
    "taiwan": "TW", "hongkong": "HK", "singapore": "SG", "australia": "AU",
    "brazil": "BR", "india": "IN", "indonesia": "ID", "vietnam": "VN",
    "thailand": "TH", "philippines": "PH", "malaysia": "MY", "greece": "GB",
    "romania": "DE", "ukraine": "PL", "portugal": "ES", "finland": "SE",
    "austria": "DE", "switzerland": "DE", "ireland": "GB", "czech": "DE",
    "hungary": "DE", "bulgaria": "DE", "serbia": "DE", "croatia": "DE",
}


@dataclass
class FingerprintIdentity:
    """一份可直接落地的指纹身份。"""

    seed: str
    persona: Persona
    args: list[str] = field(default_factory=list)
    brand_version: str = DEFAULT_BRAND_VERSION

    @property
    def label(self) -> str:
        return f"{self.persona.region}/{self.persona.platform}/{self.persona.brand} seed={self.seed}"

    def summary(self) -> str:
        return (f"{self.persona.region} {self.persona.platform} {self.persona.brand} "
                f"lang={self.persona.lang} tz={self.persona.timezone} "
                f"{self.persona.resolution} cores={self.persona.cores} seed={self.seed}")

    def to_account_data(self) -> dict[str, Any]:
        return {
            "fingerprintBrowser": "ant",
            "fingerprintSeed": self.seed,
            "fingerprintPersona": f"{self.persona.region}/{self.persona.platform}/{self.persona.brand}",
            "fingerprintTimezone": self.persona.timezone,
            "fingerprintLanguage": self.persona.lang,
        }


def choose_persona(region: str = "", platform: str = "") -> Persona:
    """按地区/平台过滤画像，找不到就全池随机。"""
    pool = list(PERSONAS)
    if region:
        narrowed = [p for p in pool if p.region == region.upper()]
        if narrowed:
            pool = narrowed
    if platform:
        narrowed = [p for p in pool if p.platform == platform.lower()]
        if narrowed:
            pool = narrowed
    return secrets.choice(pool)


def random_identity(
    *,
    region: str = "",
    platform: str = "",
    country: str = "",
    brand_version: str = "",
) -> FingerprintIdentity:
    """生成一份新的随机指纹身份（每次注册都不一样）。"""
    if not region and country:
        region = COUNTRY_REGION.get(str(country).strip().lower(), "")
    persona = choose_persona(region=region, platform=platform)
    seed = str(secrets.randbelow(899_999_999) + 100_000_000)
    identity = FingerprintIdentity(seed=seed, persona=persona,
                                   brand_version=brand_version or DEFAULT_BRAND_VERSION)
    identity.args = build_fingerprint_args(identity)
    return identity


def build_fingerprint_args(identity: FingerprintIdentity) -> list[str]:
    """转成 fingerprint-chromium 命令行参数（AntBrowser 实例的 fingerprintArgs）。"""
    persona = identity.persona
    args = [
        f"--fingerprint={identity.seed}",
        f"--fingerprint-brand={persona.brand}",
        f"--fingerprint-brand-version={identity.brand_version}",
        f"--fingerprint-platform={persona.platform}",
        f"--lang={persona.lang}",
        f"--accept-lang={persona.accept_lang}",
        f"--timezone={persona.timezone}",
        f"--window-size={persona.resolution}",
        f"--fingerprint-hardware-concurrency={persona.cores}",
        "--fingerprinting-canvas-image-data-noise",
        "--fingerprinting-client-rects-noise",
        "--disable-non-proxied-udp",
    ]
    if persona.platform_version:
        args.insert(4, f"--fingerprint-platform-version={persona.platform_version}")
    return args


def build_context_options(identity: FingerprintIdentity, *, browser_version: str = "") -> dict[str, Any]:
    """普通 Playwright 兜底路径的上下文参数（无 AntBrowser 时用）。

    只改视口 / 时区 / 语言这些不会自相矛盾的部分；UA 仅在画像平台与本机一致时才替换，
    否则「Windows UA + 真实 MacIntel 平台」这类组合反而比不伪装更可疑。
    """
    width, height = identity.persona.screen
    version = (browser_version or "").split(".")[0] or "154"
    options: dict[str, Any] = {
        "locale": identity.persona.lang,
        "timezone_id": identity.persona.timezone,
        "viewport": {"width": width, "height": height - 120},
        "device_scale_factor": 1 if identity.persona.platform != "macos" else 2,
        "color_scheme": "light",
        "extra_http_headers": {"Accept-Language": identity.persona.accept_lang.replace(",", ", ")},
    }
    if identity.persona.platform == host_platform():
        options["user_agent"] = (
            f"Mozilla/5.0 ({_ua_platform(identity.persona)}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
        )
        options["screen"] = {"width": width, "height": height}
    return options


# 连接后用这段 JS 读回真实指纹，用来确认指纹层是否真的生效。
# 关键场景：内核（普通 Chrome）会静默忽略 --fingerprint*/--timezone，
# 于是「看起来设了指纹，其实还是本机裸指纹」。这种静默失效必须被检测出来。
FINGERPRINT_PROBE_JS = """() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    webdriver: navigator.webdriver === true,
    lang: navigator.language,
    langs: Array.from(navigator.languages || []),
    timezone: (Intl.DateTimeFormat().resolvedOptions() || {}).timeZone || '',
    hardwareConcurrency: navigator.hardwareConcurrency || 0,
    screen: [window.screen.width || 0, window.screen.height || 0],
    webglRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : '',
  };
}"""

_UA_PLATFORM_HINTS = {
    "windows": ("windows",),
    "macos": ("macintosh", "mac os x"),
    "linux": ("linux", "x11"),
}
_JS_PLATFORM_HINTS = {
    "windows": ("win32", "win64", "windows"),
    "macos": ("macintel", "mac"),
    "linux": ("linux",),
}


def fingerprint_mismatches(identity: FingerprintIdentity, runtime: dict[str, Any]) -> list[str]:
    """比对画像与真实运行时值，返回不一致项（空列表 = 指纹确实生效）。"""
    issues: list[str] = []
    persona = identity.persona
    if not isinstance(runtime, dict):
        return ["无法读取运行时指纹"]

    if runtime.get("webdriver") is True:
        issues.append("navigator.webdriver=true（自动化痕迹未隐藏）")

    actual_lang = str(runtime.get("lang") or "")
    if actual_lang and actual_lang.lower() != persona.lang.lower():
        issues.append(f"语言 {actual_lang}（期望 {persona.lang}）")

    actual_tz = str(runtime.get("timezone") or "")
    if actual_tz and actual_tz != persona.timezone:
        issues.append(f"时区 {actual_tz}（期望 {persona.timezone}）")

    ua = str(runtime.get("userAgent") or "").lower()
    hints = _UA_PLATFORM_HINTS.get(persona.platform)
    if ua and hints and not any(hint in ua for hint in hints):
        issues.append(f"UA 平台与画像 {persona.platform} 不符")

    js_platform = str(runtime.get("platform") or "").lower()
    js_hints = _JS_PLATFORM_HINTS.get(persona.platform)
    if js_platform and js_hints and not any(hint in js_platform for hint in js_hints):
        issues.append(f"navigator.platform={runtime.get('platform')}（期望 {persona.platform}）")

    cores = runtime.get("hardwareConcurrency")
    if isinstance(cores, int) and cores and cores != persona.cores:
        issues.append(f"CPU 核心数 {cores}（期望 {persona.cores}）")

    return issues


def host_platform() -> str:
    """当前主机平台：windows / macos / linux。"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _ua_platform(persona: Persona) -> str:
    if persona.platform == "windows":
        return "Windows NT 10.0; Win64; x64"
    if persona.platform == "macos":
        return "Macintosh; Intel Mac OS X 10_15_7"
    return "X11; Linux x86_64"


__all__ = [
    "COUNTRY_REGION",
    "DEFAULT_BRAND_VERSION",
    "FINGERPRINT_PROBE_JS",
    "FingerprintIdentity",
    "PERSONAS",
    "Persona",
    "build_context_options",
    "build_fingerprint_args",
    "choose_persona",
    "fingerprint_mismatches",
    "host_platform",
    "random_identity",
]
