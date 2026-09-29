"""在 Playwright 上下文里落地一套自洽的指纹（CDP/上下文级 + JS 注入级）。

定位：与 AntBrowser 的内核指纹互补。
- 内核是 fingerprint-chromium（AntBrowser「内核管理」里装了指纹内核）：内核参数是主力，
  这里做一致性兜底。
- 内核是普通 Chrome（--fingerprint*/--timezone 会被静默忽略）：这里保证语言、时区、UA、
  navigator.platform、CPU 核心数、视口/屏幕、canvas 噪声、WebGL 厂商/渲染器至少与画像一致，
  不再留下「本机裸指纹」。

做法：
1. 上下文参数（Playwright 内部用 CDP Emulation 实现，对上下文内所有页面生效）：
   user_agent / locale / timezone_id / viewport / screen / device_scale_factor / Accept-Language
2. init script（页面脚本最早执行）：固定 platform、languages、hardwareConcurrency、
   deviceMemory、canvas 噪声、WebGL vendor/renderer、navigator.webdriver
"""

from __future__ import annotations

import json
from typing import Any

from core.fingerprint import FingerprintIdentity, host_platform

# 与画像平台一致的 GPU 字符串（WebGL 暴露的厂商/渲染器）
_GPU_BY_PLATFORM = {
    "windows": (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    "macos": (
        "Google Inc. (Apple)",
        "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
    ),
    "linux": (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6 (Core Profile) Mesa 22.0.5)",
    ),
}

_JS_PLATFORM = {
    "windows": "Win32",
    "macos": "MacIntel",
    "linux": "Linux x86_64",
}

_UA_PLATFORM = {
    "windows": "Windows NT 10.0; Win64; x64",
    "macos": "Macintosh; Intel Mac OS X 10_15_7",
    "linux": "X11; Linux x86_64",
}


def user_agent_for(identity: FingerprintIdentity, *, browser_version: str = "") -> str:
    version = (browser_version or "").split(".")[0] or "154"
    platform_token = _UA_PLATFORM.get(identity.persona.platform, _UA_PLATFORM["linux"])
    return (
        f"Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
    )


def context_options_for(identity: FingerprintIdentity, *, browser_version: str = "") -> dict[str, Any]:
    """上下文级伪装（Playwright 用 CDP Emulation 对上下文内所有页面生效）。"""
    persona = identity.persona
    width, height = persona.screen
    return {
        "user_agent": user_agent_for(identity, browser_version=browser_version),
        "locale": persona.lang,
        "timezone_id": persona.timezone,
        "viewport": {"width": width, "height": max(600, height - 120)},
        "screen": {"width": width, "height": height},
        "device_scale_factor": 1,
        "color_scheme": "light",
        "extra_http_headers": {"Accept-Language": persona.accept_lang.replace(",", ", ")},
    }


def init_script_for(identity: FingerprintIdentity) -> str:
    """生成 init script：在页面最早阶段把 JS 可见指纹改成画像值。"""
    persona = identity.persona
    vendor, renderer = _GPU_BY_PLATFORM.get(persona.platform, _GPU_BY_PLATFORM["linux"])
    payload = {
        "seed": int(identity.seed or 0),
        "platform": _JS_PLATFORM.get(persona.platform, "Linux x86_64"),
        "brand": persona.brand,
        "language": persona.lang,
        "languages": [part.strip() for part in persona.accept_lang.split(",") if part.strip()],
        "timezone": persona.timezone,
        "cores": persona.cores,
        "gpuVendor": vendor,
        "gpuRenderer": renderer,
        "uaPlatform": platform_of_user_agent(identity),
    }
    return _INIT_TEMPLATE.replace("__QR_PAYLOAD__", json.dumps(payload, ensure_ascii=False))


def platform_of_user_agent(identity: FingerprintIdentity) -> str:
    """UA-CH navigator.userAgentData.platform 取值。"""
    return {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(
        identity.persona.platform, "Linux"
    )


def host_identity_matches(identity: FingerprintIdentity) -> bool:
    """画像平台是否与本机一致（用于选择「只补一致性」还是「整体伪装」）。"""
    return identity.persona.platform == host_platform()


_INIT_TEMPLATE = r"""
(() => {
  const P = __QR_PAYLOAD__;
  const define = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, {get: () => value, configurable: true});
    } catch (e) {}
  };
  // 种子驱动的确定性噪声（同一账号稳定，不同账号不同）
  const noiseFrom = (seed) => {
    let x = (seed % 2147483647) || 123456789;
    return () => {
      x = (x * 48271) % 2147483647;
      return (x / 2147483647) - 0.5;
    };
  };
  const noise = noiseFrom(P.seed);

  define(navigator, 'platform', P.platform);
  define(navigator, 'language', P.language);
  define(navigator, 'languages', Object.freeze(P.languages.slice()));
  define(navigator, 'hardwareConcurrency', P.cores);
  define(navigator, 'deviceMemory', 8);
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => false, configurable: true});
  } catch (e) {}

  if (navigator.userAgentData) {
    try {
      const uad = navigator.userAgentData;
      Object.defineProperty(uad, 'platform', {get: () => P.uaPlatform, configurable: true});
      if (uad.brands) {
        Object.defineProperty(uad, 'brands', {
          get: () => ([{brand: 'Not/A)Brand', version: '8'}, {brand: 'Chromium', version: uad.brands[1] ? uad.brands[1].version : '154'}, {brand: P.brand, version: uad.brands[2] ? uad.brands[2].version : '154'}]),
          configurable: true
        });
      }
    } catch (e) {}
  }

  // WebGL 厂商/渲染器与画像平台保持一致
  const patchGL = (proto) => {
    if (!proto || !proto.getParameter) return;
    const original = proto.getParameter;
    proto.getParameter = function (name) {
      const dbg = this.getExtension && this.getExtension('WEBGL_debug_renderer_info');
      if (dbg) {
        if (name === dbg.UNMASKED_VENDOR_WEBGL) return P.gpuVendor;
        if (name === dbg.UNMASKED_RENDERER_WEBGL) return P.gpuRenderer;
      }
      return original.apply(this, arguments);
    };
  };
  patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
  patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);

  // Canvas 读数加确定性微噪声（只改读出的像素，不动页面真实画布内容）
  const jitter = (data) => {
    if (!data || !data.length) return data;
    for (let i = 0; i < data.length; i += 4) {
      const delta = noise() > 0 ? 1 : -1;
      data[i] = Math.max(0, Math.min(255, data[i] + delta));
    }
    return data;
  };
  const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
  CanvasRenderingContext2D.prototype.getImageData = function () {
    const result = origGetImageData.apply(this, arguments);
    try { jitter(result.data); } catch (e) {}
    return result;
  };
})();
"""
