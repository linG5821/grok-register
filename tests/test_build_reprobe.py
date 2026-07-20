"""build_reprobe / build_token_refresh 单测。"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

import build_reprobe as reprobe
from build_token_refresh import TokenRefreshError, refresh_access_token


class TestLoadEmails(unittest.TestCase):
    def test_parse_and_dedupe(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt") as handle:
            handle.write("# comment\n")
            handle.write("A@Example.com\n")
            handle.write("\n")
            handle.write("a@example.com\n")
            handle.write("b@x.com,password\n")
            path = handle.name
        try:
            emails = reprobe.load_emails_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(emails, ["a@example.com", "b@x.com"])


class TestIndexExport(unittest.TestCase):
    def test_index_build(self):
        exported = [
            {"provider": "grok_web", "name": "w@x.com", "access_token": "w"},
            {
                "provider": "grok_build",
                "name": "A@X.com",
                "access_token": "acc",
                "refresh_token": "ref",
            },
        ]
        index = reprobe.index_build_accounts(exported)
        self.assertIn("a@x.com", index)
        self.assertEqual(index["a@x.com"]["access_token"], "acc")
        self.assertEqual(index["a@x.com"]["refresh_token"], "ref")
        self.assertNotIn("w@x.com", index)


class TestTokenRefresh(unittest.TestCase):
    def test_success(self):
        def fake_post(url, form, **kwargs):
            self.assertEqual(form["grant_type"], "refresh_token")
            return 200, {
                "access_token": "new-acc",
                "refresh_token": "new-ref",
                "expires_in": 3600,
            }

        result = refresh_access_token("old-ref", post_form=fake_post)
        self.assertEqual(result["access_token"], "new-acc")
        self.assertEqual(result["refresh_token"], "new-ref")

    def test_permanent_fail(self):
        def fake_post(url, form, **kwargs):
            return 400, {"error": "invalid_grant", "error_description": "gone"}

        with self.assertRaises(TokenRefreshError) as ctx:
            refresh_access_token("bad", post_form=fake_post)
        self.assertTrue(ctx.exception.permanent)


class DummyResp:
    def __init__(self, code, payload=None, text=""):
        self.status_code = code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no")
        return self._payload


class TestAccountCycle(unittest.TestCase):
    def test_skipped_no_token(self):
        row = reprobe.run_account_cycle("a@b.com", None)
        self.assertEqual(row["final_status"], "skipped_no_token")

    def test_live_on_second_proxy_no_refresh(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(kwargs.get("proxies"))
            proxy = (kwargs.get("proxies") or {}).get("http") or ""
            if "p2" in proxy:
                return DummyResp(200, {"output_text": "hi"})
            return DummyResp(403, text="Access denied")

        def candidates(limit=5, exclude=None):
            return ["http://p1:1", "http://p2:2", "http://p3:3"][:limit]

        refresh_calls = []

        def fake_refresh(*a, **k):
            refresh_calls.append(1)
            raise AssertionError("should not refresh")

        row = reprobe.run_account_cycle(
            "a@b.com",
            {"access_token": "tok", "refresh_token": "ref"},
            max_proxies=5,
            http_post=fake_post,
            list_candidates=candidates,
            refresh_fn=fake_refresh,
        )
        self.assertEqual(row["final_status"], "live_proxy")
        self.assertIn("p2", row["live_proxy"])
        self.assertEqual(refresh_calls, [])
        self.assertEqual(len(row["attempts"]), 2)

    def test_refresh_then_live(self):
        phase = {"n": 0}

        def fake_post(url, **kwargs):
            phase["n"] += 1
            # phase1 all 403; after refresh first live
            if phase["n"] <= 2:
                return DummyResp(403, text="Access denied")
            return DummyResp(200, {"output_text": "ok"})

        def candidates(limit=5, exclude=None):
            base = ["http://a:1", "http://b:2", "http://c:3", "http://d:4"]
            exclude = exclude or set()
            return [u for u in base if u not in exclude][:limit]

        def fake_refresh(rt, **kwargs):
            self.assertEqual(rt, "ref")
            return {"access_token": "new", "refresh_token": "ref2"}

        row = reprobe.run_account_cycle(
            "z@z.com",
            {"access_token": "old", "refresh_token": "ref"},
            max_proxies=2,
            http_post=fake_post,
            list_candidates=candidates,
            refresh_fn=fake_refresh,
        )
        self.assertEqual(row["final_status"], "live_refresh")
        self.assertTrue(row["refreshed"])

    def test_dry_run(self):
        row = reprobe.run_account_cycle(
            "a@b.com",
            {"access_token": "t", "refresh_token": "r"},
            dry_run=True,
            list_candidates=lambda limit=5, exclude=None: ["http://x:1"],
        )
        self.assertEqual(row["final_status"], "dry_run")


if __name__ == "__main__":
    unittest.main()
