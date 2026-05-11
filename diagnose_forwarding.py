"""测 DuckDuckGo → QQ 邮箱转发链路是不是通的。

步骤：
1. 用你的 DDG token 生成一个全新私有地址
2. 让你手动用任一邮箱（QQ / Gmail / 163 都行）发一封测试邮件到这个地址
3. 脚本轮询 QQ INBOX + Junk 共 90 秒，看转发邮件到没到

用法：
    export QR_DUCK_TOKEN=...
    export QR_QQ_USER=...@qq.com
    export QR_QQ_PASS=<16位授权码>
    python3 diagnose_forwarding.py
"""

from __future__ import annotations

import email
import imaplib
import os
import sys
import time
from email.header import decode_header

from core.duck_api import generate_private_address


def dec(s: str | None) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for c, cs in parts:
        if isinstance(c, bytes):
            try:
                out.append(c.decode(cs or "utf-8", errors="replace"))
            except LookupError:
                out.append(c.decode("utf-8", errors="replace"))
        else:
            out.append(c)
    return "".join(out)


def main() -> int:
    duck_token = os.environ.get("QR_DUCK_TOKEN", "").strip()
    qq_user = os.environ.get("QR_QQ_USER", "").strip()
    qq_pass = os.environ.get("QR_QQ_PASS", "").strip()
    if not duck_token or not qq_user or not qq_pass:
        print("缺少环境变量。先设置 QR_DUCK_TOKEN / QR_QQ_USER / QR_QQ_PASS")
        return 2

    # 1) 生成新地址
    print("[1/3] 生成新 Duck 地址 ...")
    addr = generate_private_address(duck_token)
    print(f"     拿到: {addr}")

    # 2) 提示用户发测试邮件
    print()
    print("[2/3] 现在用你**任一**能发外部邮件的邮箱（QQ / Gmail / 163 / Outlook）")
    print(f"     给 {addr} 发一封测试邮件，主题/正文里**带一个 6 位数字**比如 999999")
    print("     发完按回车继续，脚本会轮询 QQ 邮箱 90 秒看转发到没到。")
    input()
    start_ts = time.time()

    # 3) 轮询 QQ
    print(f"[3/3] 开始轮询 INBOX + Junk（最多 90 秒）...")
    deadline = start_ts + 90
    seen: set[tuple[str, bytes]] = set()
    last_err: Exception | None = None
    found = False
    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL("imap.qq.com", 993, timeout=10) as M:
                M.login(qq_user, qq_pass)
                for box in ("INBOX", "Junk"):
                    typ, _ = M.select(box, readonly=True)
                    if typ != "OK":
                        continue
                    typ, data = M.search(None, "ALL")
                    if typ != "OK":
                        continue
                    uids = data[0].split()
                    for uid in reversed(uids[-30:]):
                        key = (box, uid)
                        if key in seen:
                            continue
                        seen.add(key)
                        typ, raw = M.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE TO)])")
                        if typ != "OK" or not raw or not raw[0]:
                            continue
                        msg = email.message_from_bytes(raw[0][1])
                        date_hdr = msg.get("Date", "")
                        try:
                            msg_ts = email.utils.parsedate_to_datetime(date_hdr).timestamp() if date_hdr else 0
                        except (TypeError, ValueError):
                            msg_ts = 0
                        # 仅关注脚本启动后 30 秒内之后的邮件
                        if msg_ts and msg_ts + 60 < start_ts:
                            continue
                        sender = dec(msg.get("From", ""))
                        subject = dec(msg.get("Subject", ""))
                        to = dec(msg.get("To", ""))
                        # 转发的关键特征：To 头里包含我们刚生成的地址，或 From 含 duck.com
                        match_to = addr.lower() in to.lower()
                        match_from_duck = "duck.com" in sender.lower() or "@duck" in sender.lower()
                        print(f"  {box}  uid={uid.decode():>4}  "
                              f"date={date_hdr[:25]:25}  "
                              f"from={sender[:40]:40}  "
                              f"to={to[:40]:40}  "
                              f"subj={subject[:40]}")
                        if match_to or match_from_duck:
                            print()
                            print(f"✓ 命中转发邮件（box={box}, uid={uid.decode()}）")
                            print(f"  From: {sender}")
                            print(f"  To  : {to}")
                            print(f"  Subj: {subject}")
                            print(f"  耗时: {time.time() - start_ts:.1f}s")
                            return 0
            elapsed = time.time() - start_ts
            print(f"  ... {int(elapsed)}s 暂未发现转发邮件")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  IMAP error: {e}")
        time.sleep(5)

    print()
    print("✗ 90 秒内**没**收到从 DDG 转发过来的测试邮件。")
    if last_err:
        print(f"  最后一次 IMAP 错误：{last_err}")
    print()
    print("可能性：")
    print("  - DDG 收到了邮件但没转发：账号被限流 / 转发地址不是这个 QQ")
    print("  - 你发的测试邮件根本没到 DDG（看一下你发件邮箱的「已发送」+「退信」）")
    print("  - QQ 把它在 SMTP 层就拒了（没进 INBOX 也没进 Junk）")
    print()
    print("下一步：到 https://duckduckgo.com/email/settings 看 forwarding address")
    print(f"       是不是你这个 QQ 邮箱（{qq_user}）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
