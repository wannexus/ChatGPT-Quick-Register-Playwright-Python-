"""Local settings store backed by the project `.env`.

Email providers, credentials, advanced options and proxy settings are all QR_*
entries in the git-ignored `.env`. There is no separate JSON settings file:
`config.local.json` is read exactly once to import legacy values that are not
already present in `.env`, and is otherwise never written again.

Precedence: a real shell export still wins on startup (``load_dotenv`` runs with
``override=False``), while an explicit :func:`save_config` always takes effect in
the running process and in the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv, set_key

from core.http_utils import build_proxy_url


PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_DIR / ".env"
LEGACY_JSON_PATH = PROJECT_DIR / "config.local.json"

# Variables the process was actually launched with, snapshotted before `.env` is
# ever read. Only these may override the file; everything else comes from `.env`,
# so editing or clearing a key takes effect without restarting the server.
PROCESS_ENV_KEYS: frozenset[str] = frozenset(os.environ)


DEFAULTS = {
    "duckToken": "",
    "duckBaseUrl": "",
    "icloudHost": "",
    "icloudFetchMode": "",
    "mhjcApiKey": "",
    "mhjcApiBase": "",
    "mhjcUsername": "",
    "mhjcNameStyle": "name",
    "fingerprintBrowser": "stealth",
    "qqUser": "",
    "qqPass": "",
    "qqMaxAttempts": "",
    "qqInterval": "",
    "icloudLoginTimeout": "",
    "fiveSimApiKey": "",
    "fiveSimCountry": "any",
    "fiveSimOperator": "any",
    "fiveSimProduct": "openai",
    "fiveSimMaxPrice": "",
    "fiveSimAcquirePriority": "rate",
    "fiveSimCandidateLimit": "8",
    "fiveSimAllowOtherProviders": "0",
    "fiveSimProviders": "[]",
    "fiveSimPollInterval": "2",
    "fiveSimUseProxy": "0",
    "acCheckerBaseUrl": "",
    "acCheckerPromoId": "",
    "pay153BaseUrl": "",
    "proxy": "",
    "proxyScheme": "",
    "proxyHost": "",
    "proxyPort": "",
    "proxyUser": "",
    "proxyPassword": "",
    "proxyInsecure": "",
    "proxyEnabled": "1",
    "emailSource": "",
    "codeSource": "",
    "authMode": "",
    "count": "",
    "cooldown": "",
    "browserRetry": "1",
    "headless": "",
    "noPersistent": "",
    "noClearTokens": "",
    "stopOnError": "",
    "runCodexOauth": "",
    "s2BaseUrl": "",
    "s2AdminApiKey": "",
    "s2AdminEmail": "",
    "s2AdminPassword": "",
    "s2GroupName": "codex",
    "s2Concurrency": "5",
    "s2Priority": "1",
    "s2RateMultiplier": "1",
    "s2PrivacyMode": "training_off",
}


ENV_MAP = {
    "duckToken": "QR_DUCK_TOKEN",
    "duckBaseUrl": "QR_DUCK_BASE_URL",
    "icloudHost": "QR_ICLOUD_HOST",
    "icloudFetchMode": "QR_ICLOUD_FETCH_MODE",
    "mhjcApiKey": "QR_MHJC_API_KEY",
    "mhjcApiBase": "QR_MHJC_API_BASE",
    "mhjcUsername": "QR_MHJC_USERNAME",
    "mhjcNameStyle": "QR_MHJC_NAME_STYLE",
    "fingerprintBrowser": "QR_FINGERPRINT_BROWSER",
    "qqUser": "QR_QQ_USER",
    "qqPass": "QR_QQ_PASS",
    "qqMaxAttempts": "QR_QQ_MAX_ATTEMPTS",
    "qqInterval": "QR_QQ_INTERVAL",
    "icloudLoginTimeout": "QR_ICLOUD_LOGIN_TIMEOUT",
    "fiveSimApiKey": "QR_FIVESIM_API_KEY",
    "fiveSimCountry": "QR_FIVESIM_COUNTRY",
    "fiveSimOperator": "QR_FIVESIM_OPERATOR",
    "fiveSimProduct": "QR_FIVESIM_PRODUCT",
    "fiveSimMaxPrice": "QR_FIVESIM_MAX_PRICE",
    "fiveSimAcquirePriority": "QR_FIVESIM_ACQUIRE_PRIORITY",
    "fiveSimCandidateLimit": "QR_FIVESIM_CANDIDATE_LIMIT",
    "fiveSimAllowOtherProviders": "QR_FIVESIM_ALLOW_OTHER_PROVIDERS",
    "fiveSimProviders": "QR_FIVESIM_PROVIDERS",
    "fiveSimPollInterval": "QR_FIVESIM_POLL_INTERVAL",
    "fiveSimUseProxy": "QR_FIVESIM_USE_PROXY",
    "acCheckerBaseUrl": "QR_AC_CHECKER_BASE_URL",
    "acCheckerPromoId": "QR_AC_CHECKER_PROMO_ID",
    "pay153BaseUrl": "QR_PAY153_BASE_URL",
    "proxy": "QR_PROXY",
    "proxyScheme": "QR_PROXY_SCHEME",
    "proxyHost": "QR_PROXY_HOST",
    "proxyPort": "QR_PROXY_PORT",
    "proxyUser": "QR_PROXY_USER",
    "proxyPassword": "QR_PROXY_PASSWORD",
    "proxyInsecure": "QR_PROXY_INSECURE",
    "proxyEnabled": "QR_PROXY_ENABLED",
    "emailSource": "QR_EMAIL_SOURCE",
    "codeSource": "QR_CODE_SOURCE",
    "authMode": "QR_AUTH_MODE",
    "count": "QR_COUNT",
    "cooldown": "QR_COOLDOWN",
    "browserRetry": "QR_BROWSER_RETRY",
    "headless": "QR_HEADLESS",
    "noPersistent": "QR_NO_PERSISTENT",
    "noClearTokens": "QR_NO_CLEAR_TOKENS",
    "stopOnError": "QR_STOP_ON_ERROR",
    "runCodexOauth": "QR_CODEX_OAUTH",
    "s2BaseUrl": "QR_SUB2API_BASE_URL",
    "s2AdminApiKey": "QR_SUB2API_ADMIN_API_KEY",
    "s2AdminEmail": "QR_SUB2API_ADMIN_EMAIL",
    "s2AdminPassword": "QR_SUB2API_ADMIN_PASSWORD",
    "s2GroupName": "QR_SUB2API_GROUP_NAME",
    "s2Concurrency": "QR_SUB2API_CONCURRENCY",
    "s2Priority": "QR_SUB2API_PRIORITY",
    "s2RateMultiplier": "QR_SUB2API_RATE_MULTIPLIER",
    "s2PrivacyMode": "QR_SUB2API_PRIVACY_MODE",
}


def load_env_file() -> None:
    """Load the project `.env`; real environment variables keep precedence."""
    load_dotenv(dotenv_path=ENV_PATH, override=False)


def _env_file_values() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    return {str(key): str(value or "") for key, value in (dotenv_values(ENV_PATH) or {}).items()}


def _write_env(values: dict[str, str]) -> None:
    """Upsert QR_* entries in `.env`, preserving every other line and comment.

    Unchanged keys are skipped so a full-form save does not litter `.env` with
    empty entries for settings that were never configured.
    """
    if not values:
        return
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    on_disk = _env_file_values()
    wrote = False
    for field, raw in values.items():
        env_key = ENV_MAP.get(field)
        if env_key is None:
            continue  # unknown field: never invent a key
        text = str(raw if raw is not None else "")
        # Compare against the value the app would actually use: an absent key
        # means "declared default", not "empty". Otherwise switching a default-on
        # setting off would be skipped and silently revert on the next load.
        effective = str(on_disk[env_key] or "") if env_key in on_disk else str(DEFAULTS.get(field, ""))
        if effective == text:
            continue
        set_key(str(ENV_PATH), env_key, text, quote_mode="always")
        os.environ[env_key] = text
        wrote = True
    if not wrote:
        return
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass


def migrate_legacy_json_config() -> list[str]:
    """Import legacy `config.local.json` values absent from `.env`.

    Returns the imported field names. Keys already present in `.env` are never
    overwritten, so repeated calls cannot clobber newer UI edits with stale JSON.
    The legacy file is left on disk for the operator to remove deliberately.
    """
    if not LEGACY_JSON_PATH.exists():
        return []
    try:
        data = json.loads(LEGACY_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []

    present = set(_env_file_values())
    to_import: dict[str, str] = {}
    for field in DEFAULTS:
        env_key = ENV_MAP.get(field)
        if not env_key or env_key in present:
            continue
        value = str(data.get(field, "") or "")
        if value:
            to_import[field] = value
    if not to_import:
        return []
    _write_env(to_import)
    return sorted(to_import)


def effective_config() -> dict[str, str]:
    load_env_file()
    migrate_legacy_json_config()
    file_values = _env_file_values()
    result: dict[str, str] = {}
    for field, env_key in ENV_MAP.items():
        if env_key in PROCESS_ENV_KEYS and str(os.environ.get(env_key) or ""):
            result[field] = str(os.environ[env_key])   # a real launch variable wins
        elif env_key in file_values:
            result[field] = str(file_values[env_key] or "")  # `.env` wins, even when empty
        else:
            result[field] = str(DEFAULTS.get(field, ""))     # never configured -> declared default

    # Separate host/port/user/password fields are the friendlier input; when a
    # host is present they compose the single proxy URL every consumer already
    # understands. Otherwise a full `proxy` URL keeps working unchanged.
    composed = build_proxy_url(
        host=result.get("proxyHost", ""),
        port=result.get("proxyPort", ""),
        user=result.get("proxyUser", ""),
        password=result.get("proxyPassword", ""),
        scheme=result.get("proxyScheme", "") or "http",
    )
    if composed:
        result["proxy"] = composed
    return result


def _value_to_text(value: Any) -> str:
    """Normalise a setting for `.env`: booleans become 1/empty, None becomes empty."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def save_config(values: dict[str, Any]) -> dict[str, str]:
    """Persist the provided fields into `.env`; unrecognised fields are ignored."""
    wanted = {field: _value_to_text(values[field]) for field in DEFAULTS if field in values}
    _write_env(wanted)
    return effective_config()
