"""Local secret config helpers.

Values in config.local.json are used as a fallback when QR_* environment
variables are not set. Environment variables always win.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.local.json"


DEFAULTS = {
    "duckToken": "",
    "duckBaseUrl": "",
    "qqUser": "",
    "qqPass": "",
    "proxy": "",
}


ENV_MAP = {
    "duckToken": "QR_DUCK_TOKEN",
    "duckBaseUrl": "QR_DUCK_BASE_URL",
    "qqUser": "QR_QQ_USER",
    "qqPass": "QR_QQ_PASS",
    "proxy": "QR_PROXY",
}


def load_config_file() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        return dict(DEFAULTS)
    return {**DEFAULTS, **{k: data.get(k, "") for k in DEFAULTS}}


def effective_config() -> dict[str, str]:
    file_config = load_config_file()
    result = {}
    for key, env_key in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value is None or value == "":
            value = file_config.get(key, "")
        result[key] = str(value or "")
    return result


def save_config(values: dict[str, Any]) -> dict[str, str]:
    current = load_config_file()
    for key in DEFAULTS:
        if key in values:
            current[key] = str(values.get(key) or "")
    CONFIG_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return {k: str(current.get(k, "")) for k in DEFAULTS}
