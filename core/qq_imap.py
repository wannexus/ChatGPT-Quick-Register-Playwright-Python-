"""Fetch the latest verification code from a QQ Mail inbox over IMAP.

Setup:
- Log in to https://wx.mail.qq.com/, settings > 账户 > IMAP/SMTP, enable IMAP.
- Generate an "授权码" (authorization code) — that's the IMAP password (NOT the
  QQ login password).
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header
from email.message import Message
from typing import Iterable

DEFAULT_SENDER_FILTERS = ("openai", "noreply", "verify", "auth", "duckduckgo", "forward")
DEFAULT_SUBJECT_FILTERS = ("verify", "verification", "code", "验证", "confirm")
HEADER_FETCH = "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
MESSAGE_FETCH = "(UID BODY.PEEK[])"
MAX_RECENT_UIDS_PER_BOX = 80

CODE_PATTERNS = [
    re.compile(r"(?:代码为|验证码[^0-9]*?)[\s：:]*(\d{6})"),
    re.compile(r"(?:chatgpt\s+log-?in\s+code|enter\s+this\s+code)[^0-9]{0,24}(\d{6})", re.IGNORECASE),
    re.compile(r"code[:\s]+is[:\s]+(\d{6})", re.IGNORECASE),
    re.compile(r"code[:\s]+(\d{6})", re.IGNORECASE),
    re.compile(r"\b(\d{6})\b"),
]


@dataclass
class QQImapConfig:
    user: str
    password: str  # 授权码
    host: str = "imap.qq.com"
    port: int = 993
    mailbox: str = "INBOX"
    # 也轮询的额外文件夹（垃圾箱常名）
    extra_mailboxes: tuple = ("Junk",)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


def _extract_text(msg: Message) -> str:
    if msg.is_multipart():
        bodies = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                bodies.append(payload.decode(charset, errors="replace"))
            except LookupError:
                bodies.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(bodies)
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _extract_code(text: str) -> str | None:
    cleaned = re.sub(r"<[^>]+>", " ", text)  # strip HTML tags
    for pattern in CODE_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            return m.group(1)
    return None


def _matches_filters(
    sender: str,
    subject: str,
    senders: Iterable[str],
    subjects: Iterable[str],
) -> bool:
    s = sender.lower()
    su = subject.lower()
    return any(f in s for f in senders) or any(f in su for f in subjects)


def _message_timestamp(date_header: str | None) -> float:
    if not date_header:
        return 0
    try:
        return email.utils.parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError, AttributeError):
        return 0


def _search_since_date(timestamp: float) -> str:
    # IMAP SINCE is day-granular. Search from the previous day to absorb
    # timezone differences, then apply the precise Date-header filter locally.
    return datetime.fromtimestamp(max(0, timestamp - 86400)).strftime("%d-%b-%Y")


def _uids_from_search(data: list[bytes]) -> list[bytes]:
    out: list[bytes] = []
    for chunk in data or []:
        if chunk:
            out.extend(chunk.split())
    return out


def _payload_from_fetch(raw: list[bytes | tuple]) -> bytes:
    for item in raw or []:
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    return b""


def _safe_logout(conn: imaplib.IMAP4_SSL | None) -> None:
    if conn is None:
        return
    try:
        conn.logout()
    except Exception:
        pass


def fetch_qq_code(
    config: QQImapConfig,
    *,
    max_attempts: int = 90,
    interval_seconds: float = 1.5,
    sender_filters: Iterable[str] = DEFAULT_SENDER_FILTERS,
    subject_filters: Iterable[str] = DEFAULT_SUBJECT_FILTERS,
    seen_uids: set[bytes | tuple[str, bytes]] | None = None,
    since_ts: float | None = None,
) -> str:
    """Poll the QQ inbox until a matching verification code arrives.

    `since_ts` filters by Date header so we never grab an old (pre-flow) code.
    """
    if since_ts is None:
        since_ts = time.time()
    # 时钟容错：邮件 Date 可能比 run 启动时间早 30 秒（时钟漂移、邮件传输时长）
    since_floor = max(0, since_ts - 30)
    seen = set(seen_uids or set())
    mailboxes = (config.mailbox, *config.extra_mailboxes)
    since_date = _search_since_date(since_floor)
    skipped_for_age = 0
    skipped_for_filter = 0
    skipped_no_code = 0
    unavailable_mailboxes: set[str] = set()
    senders = tuple(f.lower() for f in sender_filters)
    subjects = tuple(f.lower() for f in subject_filters)

    last_error: Exception | None = None
    conn: imaplib.IMAP4_SSL | None = None
    try:
        for attempt in range(1, max_attempts + 1):
            print(f"[qq-imap] poll {attempt}/{max_attempts}  interval={interval_seconds:g}s")
            try:
                if conn is None:
                    conn = imaplib.IMAP4_SSL(config.host, config.port)
                    conn.login(config.user, config.password)

                checked = 0
                for box in mailboxes:
                    typ, _ = conn.select(box, readonly=True)
                    if typ != "OK":
                        if box not in unavailable_mailboxes:
                            print(f"[qq-imap] skip mailbox {box!r}: select returned {typ}")
                            unavailable_mailboxes.add(box)
                        continue

                    typ, data = conn.uid("SEARCH", None, "SINCE", since_date)
                    if typ != "OK":
                        typ, data = conn.uid("SEARCH", None, "ALL")
                    if typ != "OK":
                        continue

                    uids = _uids_from_search(data)
                    for uid in reversed(uids[-MAX_RECENT_UIDS_PER_BOX:]):
                        key = (box, uid)
                        if key in seen or uid in seen:
                            continue

                        typ, raw = conn.uid("FETCH", uid, HEADER_FETCH)
                        if typ != "OK" or not raw:
                            continue
                        header_bytes = _payload_from_fetch(raw)
                        if not header_bytes:
                            continue

                        checked += 1
                        msg = email.message_from_bytes(header_bytes)
                        sender = _decode(msg.get("From"))
                        subject = _decode(msg.get("Subject"))
                        msg_ts = _message_timestamp(msg.get("Date"))
                        if msg_ts and msg_ts < since_floor:
                            seen.add(key)
                            skipped_for_age += 1
                            continue

                        code = _extract_code(subject)
                        if code:
                            print(f"[qq-imap] got code from {box}")
                            return code

                        if not _matches_filters(sender, subject, senders, subjects):
                            seen.add(key)
                            skipped_for_filter += 1
                            continue

                        typ, raw = conn.uid("FETCH", uid, MESSAGE_FETCH)
                        if typ != "OK" or not raw:
                            continue
                        body_bytes = _payload_from_fetch(raw)
                        if not body_bytes:
                            continue
                        full_msg = email.message_from_bytes(body_bytes)
                        body_text = _extract_text(full_msg)
                        code = _extract_code(f"{subject}\n{body_text}")
                        if code:
                            print(f"[qq-imap] got code from {box}")
                            return code

                        seen.add(key)
                        skipped_no_code += 1

                if checked:
                    print(f"[qq-imap] checked {checked} new message header(s)")
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as e:
                last_error = e
                print(f"[qq-imap] reconnect after IMAP error: {e}")
                _safe_logout(conn)
                conn = None
            except Exception as e:  # noqa: BLE001
                last_error = e
                print(f"[qq-imap] error: {e}")

            if attempt < max_attempts:
                time.sleep(interval_seconds)
    finally:
        _safe_logout(conn)

    extra = []
    if skipped_for_age:
        extra.append(f"过滤掉 {skipped_for_age} 封时间早于 run 启动的旧邮件")
    if skipped_for_filter:
        extra.append(f"过滤掉 {skipped_for_filter} 封发件人/主题不匹配的邮件")
    if skipped_no_code:
        extra.append(f"过滤掉 {skipped_no_code} 封未提取到验证码的候选邮件")
    extras = ("；" + "，".join(extra)) if extra else ""

    msg = f"QQ IMAP 在 {int(max_attempts * interval_seconds)} 秒内未找到匹配验证码（轮询：{', '.join(mailboxes)}）{extras}"
    if last_error:
        msg += f"；最后一次错误：{last_error}"
    raise TimeoutError(msg)
