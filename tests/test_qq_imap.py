from __future__ import annotations

import imaplib
import unittest
from unittest.mock import Mock, patch

from core.qq_imap import QQImapAuthError, QQImapConfig, check_qq_imap_login, fetch_qq_code


class QQImapTests(unittest.TestCase):
    def setUp(self):
        self.config = QQImapConfig(user="user@qq.com", password="abcdefghijklmnop")

    def test_login_check_accepts_valid_credentials(self):
        connection = Mock()
        connection.login.return_value = ("OK", [b"Success"])
        with patch("core.qq_imap.imaplib.IMAP4_SSL", return_value=connection):
            check_qq_imap_login(self.config)
        connection.login.assert_called_once_with(self.config.user, self.config.password)
        connection.logout.assert_called_once()

    def test_auth_rejection_fails_fast_without_polling(self):
        connection = Mock()
        connection.login.side_effect = imaplib.IMAP4.error(b"Login fail. password is incorrect")
        with patch("core.qq_imap.imaplib.IMAP4_SSL", return_value=connection) as factory, \
                patch("core.qq_imap.time.sleep") as sleep:
            with self.assertRaisesRegex(QQImapAuthError, "16 位授权码"):
                fetch_qq_code(self.config, max_attempts=90, interval_seconds=1.5)
        factory.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
