"""build_liveness 纯函数与探测逻辑单测。"""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock

import build_liveness as bl


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestResolveCliProfile(unittest.TestCase):
    def test_hardcoded_defaults(self):
        profile = bl.resolve_cli_profile({})
        self.assertEqual(profile["client_version"], "0.2.103")
        self.assertEqual(profile["token_auth"], "xai-grok-cli")
        self.assertEqual(profile["client_identifier"], "grok-shell")
        self.assertIn("0.2.103", profile["user_agent"])
        self.assertEqual(profile["base_url"], bl.DEFAULT_BASE_URL)

    def test_recommended_over_provider_over_config(self):
        settings = {
            "recommendedProviderBuild": {
                "clientVersion": "9.9.9",
                "userAgent": "grok-shell/9.9.9 (linux; x86_64)",
            },
            "config": {
                "providerBuild": {
                    "clientVersion": "1.0.0",
                    "userAgent": "ua-provider",
                    "clientIdentifier": "id-provider",
                    "tokenAuth": "auth-provider",
                    "baseURL": "https://provider.example/v1",
                }
            },
        }
        cfg = {
            "build_liveness_client_version": "0.0.1",
            "build_liveness_user_agent": "ua-cfg",
            "build_liveness_base_url": "https://cfg.example/v1",
            "build_liveness_token_auth": "auth-cfg",
        }
        profile = bl.resolve_cli_profile(cfg, settings)
        self.assertEqual(profile["client_version"], "9.9.9")
        self.assertEqual(profile["user_agent"], "grok-shell/9.9.9 (linux; x86_64)")
        self.assertEqual(profile["client_identifier"], "id-provider")
        self.assertEqual(profile["token_auth"], "auth-provider")
        # baseURL: config override first
        self.assertEqual(profile["base_url"], "https://cfg.example/v1")


class TestHeadersAndExtract(unittest.TestCase):
    def test_build_cli_headers_shape(self):
        profile = bl.resolve_cli_profile({})
        headers = bl.build_cli_headers("tok", profile, model="grok-4.5", user_id="u1")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(headers["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertEqual(headers["x-grok-client-mode"], "headless")
        self.assertEqual(headers["x-grok-model-override"], "grok-4.5")
        self.assertEqual(headers["x-grok-user-id"], "u1")
        self.assertTrue(headers["x-grok-session-id"])
        self.assertTrue(headers["traceparent"].startswith("00-"))

    def test_extract_output_text_variants(self):
        self.assertEqual(bl.extract_output_text({"output_text": " hi "}), "hi")
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "hello there"}],
                }
            ]
        }
        self.assertEqual(bl.extract_output_text(payload), "hello there")

    def test_classify_liveness(self):
        self.assertEqual(bl.classify_liveness(200, "", "hi"), "live")
        self.assertEqual(bl.classify_liveness(403, "Access denied", ""), "dead")
        self.assertEqual(bl.classify_liveness(200, "", ""), "error")
        self.assertEqual(bl.classify_liveness(None, error="timeout"), "error")


class TestProbe(unittest.TestCase):
    def setUp(self):
        bl.clear_cli_profile_cache()
        bl.set_liveness_output_path("")

    def test_live_success(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return DummyResponse(200, {"output_text": "Hello from grok"})

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "liveness.jsonl")
            result = bl.probe_build_responses(
                "access-token",
                proxy="socks5://127.0.0.1:1080",
                config={"build_liveness_model": "grok-4.5"},
                http_post=fake_post,
                email="a@example.com",
                output_path=path,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "live")
            self.assertEqual(result["preview"], "Hello from grok")
            self.assertEqual(len(calls), 1)
            url, kwargs = calls[0]
            self.assertTrue(url.endswith("/responses"))
            self.assertEqual(kwargs["json"]["model"], "grok-4.5")
            self.assertEqual(kwargs["json"]["input"], "hi")
            self.assertEqual(kwargs["proxies"]["http"], "socks5://127.0.0.1:1080")
            self.assertFalse(kwargs["force_direct"])
            self.assertIn("xai-grok-cli", kwargs["headers"]["X-XAI-Token-Auth"])
            with open(path, "r", encoding="utf-8") as handle:
                row = json.loads(handle.readline())
            self.assertEqual(row["email"], "a@example.com")
            self.assertTrue(row["ok"])

    def test_dead_403(self):
        def fake_post(url, **kwargs):
            return DummyResponse(403, text="Access denied")

        result = bl.probe_build_responses("tok", http_post=fake_post, email="b@x.com")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "dead")

    def test_no_token(self):
        result = bl.probe_build_responses("", http_post=lambda *a, **k: None)
        self.assertEqual(result["error"], "no_build_token")
        self.assertEqual(result["status"], "error")

    def test_timeout_error(self):
        def fake_post(url, **kwargs):
            raise TimeoutError("timed out")

        result = bl.probe_build_responses("tok", http_post=fake_post)
        self.assertEqual(result["status"], "error")
        self.assertIn("timed out", result["error"])

    def test_liveness_path_for_accounts(self):
        path = bl.liveness_path_for_accounts("/tmp/out/accounts_20260719_120000.txt")
        self.assertTrue(path.endswith("liveness_20260719_120000.jsonl"))
        self.assertIn(os.path.join("tmp", "out") if os.sep == "\\" else "/tmp/out", path.replace("\\", "/"))


class TestProfileCache(unittest.TestCase):
    def setUp(self):
        bl.clear_cli_profile_cache()

    def test_cache_ttl(self):
        fetches = []

        def fetch():
            fetches.append(1)
            return {
                "recommendedProviderBuild": {
                    "clientVersion": "8.8.8",
                    "userAgent": "ua-8",
                }
            }

        p1 = bl.get_cached_cli_profile(
            {"build_liveness_fetch_cli_from_chenyme": True, "build_liveness_cli_cache_ttl_sec": 3600},
            fetch_settings=fetch,
            now=1000.0,
        )
        p2 = bl.get_cached_cli_profile(
            {"build_liveness_fetch_cli_from_chenyme": True, "build_liveness_cli_cache_ttl_sec": 3600},
            fetch_settings=fetch,
            now=1001.0,
        )
        self.assertEqual(p1["client_version"], "8.8.8")
        self.assertEqual(p2["client_version"], "8.8.8")
        self.assertEqual(len(fetches), 1)


if __name__ == "__main__":
    unittest.main()
