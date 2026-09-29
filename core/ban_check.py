"""封禁检测：从 OpenAI 通知邮件和登录页面识别「账号已被停用」信号。

两个信号源共用这里的判定规则，避免两处关键词各自漂移：

* 邮箱扫描（``core/qq_imap.py`` / ``core/mhjc.py`` 的 ``scan_*_messages``）：
  OpenAI 停用账号时会发通知邮件，按收件人匹配到 MySQL 账号后标记。
* 登录/刷新页面（``core/flow.py``）：被停用的账号在登录时直接显示停用页，
  以前会被当成「等下一步超时」，现在会立刻报出真实原因。

模块本身是纯函数，不碰网络也不碰数据库，便于单测。
"""

from __future__ import annotations

import email
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

# 「账号已被停用/封禁」的强特征。命中即判定，避免依赖多个弱特征组合。
BAN_PATTERNS = (
    re.compile(r"account\s+(?:has\s+been\s+|was\s+|is\s+)?(?:deactivated|disabled|suspended|terminated|banned)\b", re.IGNORECASE),
    re.compile(r"\b(?:deactivated|disabled|suspended|terminated|banned)\s+your\s+account\b", re.IGNORECASE),
    re.compile(r"violation\s+of\s+(?:our\s+|the\s+)?(?:usage\s+policies|terms\s+of\s+use|policies|terms)", re.IGNORECASE),
    re.compile(r"account\s+(?:deactivation|suspension|termination)\b", re.IGNORECASE),
    re.compile(r"账号\s*(?:已|已经)?\s*(?:被|遭)?\s*(?:停用|封禁|封号|禁用|冻结)"),
    re.compile(r"(?:帐号|账户)\s*(?:已|已经)?\s*(?:被|遭)?\s*(?:停用|封禁|封号|禁用|冻结)"),
    re.compile(r"账号\s*(?:已|已经)?\s*(?:被|遭)?\s*删除"),
    re.compile(r"账户\s*(?:已|已经)?\s*(?:被|遭)?\s*删除"),
    re.compile(r"(?!错误|失败)[^。\n]{0,80}(?:没有|无)账户[^。\n]{0,120}(?:已被删除|已停用|被停用|停用)"),
    re.compile(r"account_deactivated", re.IGNORECASE),
    re.compile(r"违反\s*(?:使用)?\s*(?:政策|条款|规定)"),
)

# 单封信最多保留的正文/证据长度，避免把整封邮件塞进 MySQL。
MAX_BODY_CHARS = 4000
MAX_EVIDENCE_CHARS = 500


def _decode_header_value(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001
        return str(value)


def _body_text(msg: Message) -> str:
    """Return the message body as text, plain-text parts before HTML ones.

    HTML-only notices are common, so both are collected instead of stopping at
    the first text part; that costs nothing and avoids missing a ban notice.
    """
    plain: List[str] = []
    html: List[str] = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            html.append(re.sub(r"<[^>]+>", " ", text))
        else:
            plain.append(text)
    return "\n".join(plain + html).strip()[:MAX_BODY_CHARS]


def _message_timestamp(date_header: Optional[str]) -> float:
    if not date_header:
        return 0.0
    try:
        return parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0.0


def message_to_record(raw: bytes) -> Dict[str, Any]:
    """Turn one raw RFC822 message into the small record the scanner works on."""
    record: Dict[str, Any] = {"subject": "", "to": "", "body": "", "date": "", "date_ts": 0.0}
    try:
        msg = email.message_from_bytes(raw or b"")
    except Exception:  # noqa: BLE001
        return record
    try:
        record["subject"] = _decode_header_value(msg.get("Subject"))
        record["to"] = _decode_header_value(msg.get("To"))
        record["date"] = _decode_header_value(msg.get("Date"))
        record["date_ts"] = _message_timestamp(msg.get("Date"))
        record["body"] = _body_text(msg)[:MAX_BODY_CHARS]
    except Exception:  # noqa: BLE001
        pass
    return record


def classify_ban_notice(subject: str, body: str = "") -> Optional[str]:
    """Return the matched ban phrase, or None when this is ordinary mail.

    Only strong phrases count: a login alert about "unusual activity" or a
    newsletter must never mark a healthy account as banned.
    """
    haystack = f"{subject or ''}\n{body or ''}"
    for pattern in BAN_PATTERNS:
        match = pattern.search(haystack)
        if match:
            return match.group(0).strip()
    return None


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _recipients(record: Mapping[str, Any]) -> set[str]:
    return {addr.lower() for addr in _EMAIL_IN_TEXT.findall(str(record.get("to") or ""))}


def find_banned_accounts(
    records: Iterable[Mapping[str, Any]],
    accounts: Iterable[Mapping[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    """Match ban notices to MySQL accounts by recipient address.

    Returns ``{account_id: evidence}``; the newest notice per account wins.
    """
    by_email = {
        str(account.get("email") or "").strip().lower(): int(account["id"])
        for account in accounts
        if str(account.get("email") or "").strip() and account.get("id") is not None
    }
    found: Dict[int, Dict[str, Any]] = {}
    for record in records:
        reason = classify_ban_notice(str(record.get("subject") or ""), str(record.get("body") or ""))
        if not reason:
            continue
        for address in _recipients(record):
            account_id = by_email.get(address)
            if account_id is None:
                continue
            evidence = {
                "reason": reason,
                "subject": str(record.get("subject") or ""),
                "date": str(record.get("date") or ""),
                "detectedAt": None,  # filled by the caller so the clock stays injectable
                "source": "email",
                "evidence": str(record.get("body") or "").strip()[:MAX_EVIDENCE_CHARS],
                "_date_ts": float(record.get("date_ts") or 0.0),
            }
            previous = found.get(account_id)
            if previous is None or evidence["_date_ts"] >= previous["_date_ts"]:
                found[account_id] = evidence
    for entry in found.values():
        entry.pop("_date_ts", None)
    return found


__all__ = [
    "BAN_PATTERNS",
    "MAX_BODY_CHARS",
    "classify_ban_notice",
    "find_banned_accounts",
    "message_to_record",
]
