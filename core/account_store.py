"""MySQL account-store configuration, field encryption, and local checks.

Persistence operations live in this module so callers do not create a second
account source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv


_DATABASE_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_REQUIRED_ENV = (
    "QR_MYSQL_HOST",
    "QR_MYSQL_USER",
    "QR_MYSQL_DATABASE",
    "QR_ACCOUNT_ENCRYPTION_KEY",
)


@dataclass(frozen=True)
class AccountStoreConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    encryption_key: str


def _fernet(key: str | bytes) -> Fernet:
    try:
        raw = key.encode("ascii") if isinstance(key, str) else bytes(key)
        return Fernet(raw)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("QR_ACCOUNT_ENCRYPTION_KEY is not a valid Fernet key") from exc


def load_config(
    *,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> AccountStoreConfig:
    """Load DB settings and validate the required password-encryption key.

    Explicit ``env`` values are useful for tests and intentionally do not read
    a developer's real `.env` file. With no mapping, `.env` is loaded once from
    the project root and process environment values take precedence.
    """
    if env is None:
        project_root = Path(__file__).resolve().parents[1]
        load_dotenv(dotenv_path=Path(env_file) if env_file else project_root / ".env", override=False)
        values: Mapping[str, str] = os.environ
    else:
        values = env

    missing = [name for name in _REQUIRED_ENV if not str(values.get(name, "")).strip()]
    if missing:
        raise ValueError("缺少必要环境变量：" + ", ".join(missing))

    database = str(values["QR_MYSQL_DATABASE"]).strip()
    if not _DATABASE_NAME_RE.fullmatch(database):
        raise ValueError("QR_MYSQL_DATABASE 只允许字母、数字和下划线")

    raw_port = str(values.get("QR_MYSQL_PORT", "3306")).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("QR_MYSQL_PORT 必须为 1-65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("QR_MYSQL_PORT 必须为 1-65535 的整数")

    key = str(values["QR_ACCOUNT_ENCRYPTION_KEY"]).strip()
    _fernet(key)
    return AccountStoreConfig(
        host=str(values["QR_MYSQL_HOST"]).strip(),
        port=port,
        user=str(values["QR_MYSQL_USER"]).strip(),
        password=str(values.get("QR_MYSQL_PASSWORD", "")),
        database=database,
        encryption_key=key,
    )


def encrypt_password(password: str, key: str | bytes) -> bytes:
    """Return Fernet ciphertext; plaintext is never persisted by this helper."""
    return _fernet(key).encrypt(str(password or "").encode("utf-8"))


def decrypt_password(ciphertext: str | bytes, key: str | bytes) -> str:
    """Decrypt an account password only for in-process login use."""
    token = ciphertext.encode("ascii") if isinstance(ciphertext, str) else bytes(ciphertext)
    try:
        return _fernet(key).decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("账号密码密文无法解密；请核对 .env 中的 QR_ACCOUNT_ENCRYPTION_KEY") from exc


def _parse_expiry(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def check_local_validity(
    account: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify locally stored ChatGPT session metadata; never make a request."""
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session = account.get("session")
    if not isinstance(session, dict):
        return {
            "status": "missing_session",
            "checkedAt": checked_at.isoformat(),
            "expiresAt": None,
            "message": "未找到本地 session；此检查不访问远端服务。",
        }

    token = str(session.get("accessToken") or "").strip()
    if not token:
        return {
            "status": "missing_token",
            "checkedAt": checked_at.isoformat(),
            "expiresAt": None,
            "message": "本地 session 中没有 access token；此检查不访问远端服务。",
        }

    expiry_value = session.get("expires") or session.get("expiresAt") or session.get("expires_at")
    expiry = _parse_expiry(expiry_value)
    if expiry is None:
        return {
            "status": "unknown_expiry",
            "checkedAt": checked_at.isoformat(),
            "expiresAt": None,
            "message": "仅依据本地 session 字段检查；未提供可解析的过期时间，无法据此判断远端可用性。",
        }

    status = "expired" if expiry <= checked_at else "not_expired"
    return {
        "status": status,
        "checkedAt": checked_at.isoformat(),
        "expiresAt": expiry.isoformat(),
        "message": "仅依据本地 session 过期时间判断，不代表远端验证结果。",
    }


class AccountStore:
    """The sole SQL owner for durable account state."""

    def __init__(self, config: AccountStoreConfig | None = None, *, connection_factory=None):
        self.config = config or load_config()
        self._connection_factory = connection_factory

    def _connect(self, *, database: bool = True):
        import pymysql
        from pymysql.cursors import DictCursor

        factory = self._connection_factory or pymysql.connect
        options = {
            "host": self.config.host,
            "port": self.config.port,
            "user": self.config.user,
            "password": self.config.password,
            "charset": "utf8mb4",
            "connect_timeout": 10,
            "autocommit": False,
            "cursorclass": DictCursor,
        }
        if database:
            options["database"] = self.config.database
        return factory(**options)

    def initialize(self) -> dict[str, Any]:
        """Create the configured database and accounts table; never import files."""
        database = self.config.database
        if not _DATABASE_NAME_RE.fullmatch(database):
            raise ValueError("QR_MYSQL_DATABASE 只允许字母、数字和下划线")
        connection = self._connect(database=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                cursor.execute(f"USE `{database}`")
                cursor.execute(
                    """CREATE TABLE IF NOT EXISTS accounts (
                        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                        email VARCHAR(320) NOT NULL,
                        password_ciphertext VARBINARY(1024) NOT NULL,
                        account_data JSON NOT NULL,
                        relogin_required TINYINT(1) NOT NULL DEFAULT 0,
                        validity_status VARCHAR(32) NOT NULL DEFAULT 'unknown_expiry',
                        validity_checked_at DATETIME(6) NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (id),
                        UNIQUE KEY uq_accounts_email (email),
                        KEY ix_accounts_relogin (relogin_required, updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.check_ready()

    def check_ready(self) -> dict[str, Any]:
        """Read-only connection and schema readiness probe."""
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 AS ready")
                result = cursor.fetchone()
                cursor.execute("SHOW TABLES LIKE %s", ("accounts",))
                table = cursor.fetchone()
            return {
                "ok": bool(result and table),
                "database": self.config.database,
                "table": "accounts" if table else None,
            }
        finally:
            connection.close()

    @staticmethod
    def _json_data(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _date_value(value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value) if value is not None else None

    def _row_to_account(self, row: Mapping[str, Any] | None, *, include_password: bool = False) -> dict[str, Any] | None:
        if not row:
            return None
        data = self._json_data(row.get("account_data"))
        email = str(row.get("email") or data.get("email") or "").strip()
        data.pop("password", None)
        data["id"] = int(row["id"])
        data["email"] = email
        data["reloginRequired"] = bool(row.get("relogin_required"))
        validity = check_local_validity(data)
        validity["checkedAt"] = self._date_value(row.get("validity_checked_at"))
        data["localValidity"] = validity
        data["passwordStored"] = bool(row.get("password_ciphertext"))
        data["createdAt"] = self._date_value(row.get("created_at"))
        data["updatedAt"] = self._date_value(row.get("updated_at"))
        if include_password:
            data["password"] = decrypt_password(row.get("password_ciphertext") or b"", self.config.encryption_key)
        return data

    def count_codex_phone_accounts(self, phone: str) -> int:
        """Return distinct account rows bound to ``phone`` (no external pool counts)."""
        if not str(phone or "").strip():
            return 0
        normalized = self._normalize_phone(phone)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(DISTINCT id) AS account_count FROM accounts "
                    "WHERE JSON_UNQUOTE(JSON_EXTRACT(account_data, '$.codexPhoneNumber')) = %s",
                    (normalized,),
                )
                row = cursor.fetchone() or {}
                return int(row.get("account_count") or 0)
        finally:
            connection.close()

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        normalized = str(phone or "").strip()
        normalized = "+" + normalized.lstrip("+")
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", normalized):
            raise ValueError("Codex 手机号必须为有效的 E.164 格式")
        return normalized

    @contextmanager
    def reserve_codex_phone(self, account_id: int, phone: str, *, max_accounts: int = 3,
                           allow_existing: bool = False):
        """Hold account/phone advisory locks across verification; yield a commit callback.

        A full phone yields None. No row lock or open transaction is held while
        waiting for SMS. Only the callback writes the accepted OTP binding.
        """
        normalized = self._normalize_phone(phone)
        limit = max(1, int(max_accounts))
        lock_names = [
            f"codex-account:{int(account_id)}",
            "codex-phone:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48],
        ]
        connection = self._connect()
        acquired = []
        try:
            with connection.cursor() as cursor:
                for lock_name in lock_names:
                    cursor.execute("SELECT GET_LOCK(%s, 15) AS acquired", (lock_name,))
                    if not (cursor.fetchone() or {}).get("acquired"):
                        raise TimeoutError("手机号绑定锁等待超时；当前账号跳过")
                    acquired.append(lock_name)
                cursor.execute(
                    "SELECT id, account_data FROM accounts WHERE id = %s",
                    (int(account_id),),
                )
                row = cursor.fetchone()
                if not row:
                    raise KeyError(f"MySQL 中不存在账号 ID={int(account_id)}")
                data = self._json_data(row.get("account_data"))
                existing = str(data.get("codexPhoneNumber") or "").strip()
                if existing and (existing != normalized or not allow_existing):
                    raise ValueError("该账号已有 Codex 手机号绑定，请先核对账号；不再购买临时号码")
                count = self._count_codex_phone_accounts(cursor, normalized)
            connection.commit()
            if count >= limit and existing != normalized:
                yield None
                return

            def commit_binding():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT id, account_data FROM accounts WHERE id = %s FOR UPDATE",
                                   (int(account_id),))
                    current = cursor.fetchone()
                    if not current:
                        raise KeyError(f"MySQL 中不存在账号 ID={int(account_id)}")
                    payload = self._json_data(current.get("account_data"))
                    old_phone = str(payload.get("codexPhoneNumber") or "").strip()
                    if old_phone and old_phone != normalized:
                        raise ValueError("该账号已绑定其他 Codex 手机号")
                    owners = self._count_codex_phone_accounts(cursor, normalized)
                    if not old_phone and owners >= limit:
                        raise RuntimeError("号码绑定名额已满；请核对账号记录")
                    if not old_phone:
                        payload["codexPhoneNumber"] = normalized
                        cursor.execute(
                            "UPDATE accounts SET account_data = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), int(account_id)),
                        )
                connection.commit()
                return {"ok": True, "already_bound": bool(old_phone),
                        "account_count": owners if old_phone else owners + 1, "phone": normalized}

            yield commit_binding
        finally:
            try:
                connection.rollback()
            finally:
                try:
                    with connection.cursor() as cursor:
                        for lock_name in reversed(acquired):
                            cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
                finally:
                    connection.close()

    def bind_codex_phone(self, account_id: int, phone: str, *, max_accounts: int = 3) -> dict[str, Any]:
        """Atomically bind an already verified phone, including idempotent repeats."""
        with self.reserve_codex_phone(account_id, phone, max_accounts=max_accounts,
                                     allow_existing=True) as bind:
            if bind is None:
                return {"ok": False, "already_bound": False, "account_count": max_accounts,
                        "phone": self._normalize_phone(phone)}
            return bind()

    @staticmethod
    def _count_codex_phone_accounts(cursor, phone: str) -> int:
        cursor.execute(
            "SELECT COUNT(DISTINCT id) AS account_count FROM accounts "
            "WHERE JSON_UNQUOTE(JSON_EXTRACT(account_data, '$.codexPhoneNumber')) = %s",
            (phone,),
        )
        row = cursor.fetchone() or {}
        return int(row.get("account_count") or 0)

    def save_account(self, email: str, password: str, account_data: Mapping[str, Any]) -> dict[str, Any]:
        normalized_email = str(email or "").strip()
        if not normalized_email:
            raise ValueError("账号 email 不能为空")
        data = dict(account_data)
        data.pop("password", None)
        # Binding writes belong exclusively to reserve_codex_phone's OTP commit.
        data.pop("codexPhoneNumber", None)
        data["email"] = normalized_email
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        ciphertext = encrypt_password(password, self.config.encryption_key)
        relogin_required = int(bool(data.get("reloginRequired")))
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO accounts (email, password_ciphertext, account_data, relogin_required)
                       VALUES (%s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                         password_ciphertext = VALUES(password_ciphertext),
                         account_data = CASE
                           WHEN JSON_EXTRACT(account_data, '$.codexPhoneNumber') IS NOT NULL
                           THEN JSON_SET(VALUES(account_data), '$.codexPhoneNumber',
                                         JSON_EXTRACT(account_data, '$.codexPhoneNumber'))
                           ELSE VALUES(account_data) END,
                         relogin_required = VALUES(relogin_required),
                         updated_at = CURRENT_TIMESTAMP""",
                    (normalized_email, ciphertext, payload, relogin_required),
                )
                cursor.execute(
                    "SELECT id, email, password_ciphertext, account_data, relogin_required, "
                    "validity_status, validity_checked_at, created_at, updated_at "
                    "FROM accounts WHERE email = %s",
                    (normalized_email,),
                )
                row = cursor.fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        saved = self._row_to_account(row)
        if saved is None:
            raise RuntimeError("MySQL 写入后未能读取账号记录")
        return saved

    def get_account(self, account_id: int, *, include_password: bool = False) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, email, password_ciphertext, account_data, relogin_required, "
                    "validity_status, validity_checked_at, created_at, updated_at "
                    "FROM accounts WHERE id = %s",
                    (int(account_id),),
                )
                row = cursor.fetchone()
            return self._row_to_account(row, include_password=include_password)
        finally:
            connection.close()

    def list_accounts(self, *, include_password: bool = False) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id, email, password_ciphertext, account_data, relogin_required, "
                    "validity_status, validity_checked_at, created_at, updated_at "
                    "FROM accounts ORDER BY updated_at DESC, id DESC"
                )
                rows = cursor.fetchall()
            return [
                account
                for row in rows
                if (account := self._row_to_account(row, include_password=include_password)) is not None
            ]
        finally:
            connection.close()

    def delete_account(self, account_id: int) -> bool:
        """Remove exactly one account row; returns whether a row was removed."""
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM accounts WHERE id = %s", (int(account_id),))
                removed = int(getattr(cursor, "rowcount", 0) or 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return removed > 0

    def record_validity(self, account_id: int, validity: Mapping[str, Any]) -> dict[str, Any] | None:
        """Persist a local-only validity result for one account; never contacts a remote service."""
        status = str(validity.get("status") or "unknown_expiry")
        checked_at = _parse_expiry(validity.get("checkedAt")) or datetime.now(timezone.utc)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE accounts SET validity_status = %s, validity_checked_at = %s WHERE id = %s",
                    (status, checked_at.replace(tzinfo=None), int(account_id)),
                )
                updated = int(getattr(cursor, "rowcount", 0) or 0)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if not updated:
            return None
        return self.get_account(int(account_id))


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m core.account_store")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the configured database and accounts table")
    commands.add_parser("check", help="verify MySQL connectivity and the accounts table")
    args = parser.parse_args(argv)
    try:
        store = AccountStore()
        result = store.initialize() if args.command == "init" else store.check_ready()
        if not result.get("ok"):
            print("[account-store] 数据库或 accounts 表未就绪", file=sys.stderr)
            return 1
        print(f"[account-store] ready database={result['database']} table=accounts")
        return 0
    except Exception:  # noqa: BLE001
        print("[account-store] 操作失败；请检查 .env 配置与数据库连接", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AccountStore",
    "AccountStoreConfig",
    "check_local_validity",
    "decrypt_password",
    "encrypt_password",
    "load_config",
]
