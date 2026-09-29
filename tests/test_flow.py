from __future__ import annotations

import unittest

from core.flow import _is_email_submission_progress_url


class EmailSubmissionProgressUrlTests(unittest.TestCase):
    def test_chatgpt_email_query_is_progress(self):
        self.assertTrue(
            _is_email_submission_progress_url(
                "https://chatgpt.com/auth/login?screen_hint=signup&email=user%40example.com"
            )
        )

    def test_signup_hint_without_email_is_not_progress(self):
        self.assertFalse(
            _is_email_submission_progress_url("https://chatgpt.com/auth/login?screen_hint=signup")
        )

    def test_auth_and_verification_destinations_are_progress(self):
        self.assertTrue(_is_email_submission_progress_url("https://auth.openai.com/u/login"))
        self.assertTrue(_is_email_submission_progress_url("https://chatgpt.com/email-verification"))
        self.assertTrue(_is_email_submission_progress_url("https://chatgpt.com/auth/password"))


if __name__ == "__main__":
    unittest.main()
