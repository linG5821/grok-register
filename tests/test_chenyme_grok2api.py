import unittest
from unittest.mock import Mock, patch

import grok_register_ttk as app


class DummyResponse:
    def __init__(self, payload=None, status_code=200, reason="", text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.reason = reason
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}: {self.reason}")

    def json(self):
        return self._payload


class DummyCurlMime:
    def __init__(self):
        self.parts = []

    def addpart(self, **kwargs):
        self.parts.append(kwargs)
        return self

    def close(self):
        pass


class ChenymeGrok2ApiTests(unittest.TestCase):
    def setUp(self):
        self.original_config = app.config.copy()
        app.chenyme_clear_token_cache()
        app.config.update({
            "chenyme_grok2api_enabled": True,
            "chenyme_grok2api_base": "http://192.168.8.228:31101",
            "chenyme_grok2api_username": "admin",
            "chenyme_grok2api_password": "secret",
            "chenyme_grok2api_convert": True,
            "chenyme_grok2api_convert_strategy": "missing",
            "build_liveness_enabled": True,
        })

    def tearDown(self):
        app.config = self.original_config
        app.chenyme_clear_token_cache()

    def test_disabled_skips_http(self):
        app.config["chenyme_grok2api_enabled"] = False
        with patch.object(app, "remote_import_http_post") as mock_post, \
                patch.object(app.requests, "post") as mock_req:
            ok = app.add_token_to_chenyme_grok2api("sso=abc123", email="a@example.com")
        self.assertFalse(ok)
        mock_post.assert_not_called()
        mock_req.assert_not_called()

    def test_missing_config_skips(self):
        app.config["chenyme_grok2api_base"] = ""
        with patch.object(app, "remote_import_http_post") as mock_post, \
                patch.object(app.requests, "post") as mock_req:
            ok = app.add_token_to_chenyme_grok2api("sso=abc123", email="a@example.com")
        self.assertFalse(ok)
        mock_post.assert_not_called()
        mock_req.assert_not_called()

    def test_login_and_cache(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return DummyResponse({
                "data": {
                    "tokens": {
                        "accessToken": "token-1",
                        "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                    }
                }
            })

        with patch.object(app, "remote_import_http_post", side_effect=fake_post):
            t1 = app.chenyme_get_access_token()
            t2 = app.chenyme_get_access_token()

        self.assertEqual(t1, "token-1")
        self.assertEqual(t2, "token-1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith("/api/admin/v1/auth/login"))

    def test_import_multipart_sends_pure_sso(self):
        req_calls = []

        def fake_req_post(url, **kwargs):
            req_calls.append((url, kwargs))
            if url.endswith("/auth/login"):
                return DummyResponse({
                    "data": {
                        "tokens": {
                            "accessToken": "jwt-abc",
                            "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                        }
                    }
                })
            return DummyResponse(text="event: done\ndata: ok\n\n")

        def fake_http_post(url, **kwargs):
            req_calls.append((url, kwargs))
            return DummyResponse({
                "data": {
                    "tokens": {
                        "accessToken": "jwt-abc",
                        "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                    }
                }
            })

        fake_mime = DummyCurlMime()
        with patch.object(app, "CurlMime", return_value=fake_mime), \
                patch.object(app.requests, "post", side_effect=fake_req_post), \
                patch.object(app, "remote_import_http_post", side_effect=fake_http_post):
            ok = app.chenyme_import_sso("sso=rawtokenvalue")

        self.assertTrue(ok)
        import_calls = [c for c in req_calls if c[0].endswith("/accounts/web/import")]
        self.assertEqual(len(import_calls), 1)
        url, kwargs = import_calls[0]
        self.assertEqual(url, "http://192.168.8.228:31101/api/admin/v1/accounts/web/import")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-abc")
        self.assertEqual(kwargs["timeout"], 60)
        self.assertIn("multipart", kwargs)
        # 默认 remote_import_use_proxy=false → multipart 直连
        self.assertEqual(kwargs.get("proxies"), {})
        self.assertEqual(len(fake_mime.parts), 1)
        self.assertEqual(fake_mime.parts[0]["name"], "files")
        self.assertEqual(fake_mime.parts[0]["filename"], "grok-web-sso-tokens.txt")
        self.assertEqual(fake_mime.parts[0]["content_type"], "text/plain")
        self.assertEqual(fake_mime.parts[0]["data"], b"rawtokenvalue")

    def test_import_401_refreshes_and_retries(self):
        login_count = {"n": 0}
        import_status = {"n": 0}
        req_calls = []

        def fake_req_post(url, **kwargs):
            req_calls.append(url)
            if url.endswith("/auth/login"):
                login_count["n"] += 1
                return DummyResponse({
                    "data": {
                        "tokens": {
                            "accessToken": f"jwt-{login_count['n']}",
                            "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                        }
                    }
                })
            if url.endswith("/accounts/web/import"):
                import_status["n"] += 1
                if import_status["n"] == 1:
                    return DummyResponse(status_code=401, reason="Unauthorized")
                return DummyResponse(text="ok")
            return DummyResponse(status_code=404)

        fake_mime = DummyCurlMime()
        with patch.object(app, "CurlMime", return_value=fake_mime), \
                patch.object(app.requests, "post", side_effect=fake_req_post), \
                patch.object(app, "remote_import_http_post", side_effect=fake_req_post):
            ok = app.chenyme_import_sso("token-xyz")

        self.assertTrue(ok)
        self.assertEqual(login_count["n"], 2)
        self.assertEqual(import_status["n"], 2)

    def test_convert_body(self):
        post_calls = []

        def fake_post(url, **kwargs):
            post_calls.append((url, kwargs))
            if url.endswith("/auth/login"):
                return DummyResponse({
                    "data": {
                        "tokens": {
                            "accessToken": "jwt-c",
                            "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                        }
                    }
                })
            return DummyResponse(text="done")

        with patch.object(app, "remote_import_http_post", side_effect=fake_post):
            ok = app.chenyme_convert_to_build()

        self.assertTrue(ok)
        convert_calls = [c for c in post_calls if c[0].endswith("/accounts/web/convert-to-build")]
        self.assertEqual(len(convert_calls), 1)
        _, kwargs = convert_calls[0]
        self.assertEqual(kwargs["json"], {"all": True, "strategy": "missing"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-c")

    def test_orchestrator_import_and_convert(self):
        post_calls = []
        req_calls = []

        def fake_http_post(url, **kwargs):
            post_calls.append(url)
            if url.endswith("/auth/login"):
                return DummyResponse({
                    "data": {
                        "tokens": {
                            "accessToken": "jwt-o",
                            "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                        }
                    }
                })
            return DummyResponse(text="ok")

        def fake_req_post(url, **kwargs):
            req_calls.append(url)
            return DummyResponse(text="ok")

        fake_mime = DummyCurlMime()
        with patch.object(app, "CurlMime", return_value=fake_mime), \
                patch.object(app.requests, "post", side_effect=fake_req_post), \
                patch.object(app, "remote_import_http_post", side_effect=fake_http_post), \
                patch.object(app, "chenyme_export_accounts", return_value=[]), \
                patch.object(app, "chenyme_check_bot_flag", return_value=None), \
                patch.object(app, "chenyme_probe_build_liveness", return_value={"ok": True, "skipped": False}) as probe:
            ok = app.add_token_to_chenyme_grok2api("sso=abc", email="a@example.com")

        self.assertTrue(ok)
        self.assertTrue(any(u.endswith("/auth/login") for u in post_calls))
        self.assertTrue(any(u.endswith("/accounts/web/import") for u in req_calls))
        self.assertTrue(any(u.endswith("/accounts/web/convert-to-build") for u in post_calls))
        probe.assert_called_once()

    def test_import_multipart_uses_proxy_when_enabled(self):
        app.config["remote_import_use_proxy"] = True
        req_calls = []

        def fake_req_post(url, **kwargs):
            req_calls.append((url, kwargs))
            return DummyResponse(text="ok")

        def fake_login(url, **kwargs):
            return DummyResponse({
                "data": {
                    "tokens": {
                        "accessToken": "jwt-p",
                        "accessTokenExpiresAt": "2099-01-01T00:00:00Z",
                    }
                }
            })

        fake_mime = DummyCurlMime()
        with patch.object(app, "CurlMime", return_value=fake_mime), \
                patch.object(app.requests, "post", side_effect=fake_req_post), \
                patch.object(app, "remote_import_http_post", side_effect=fake_login):
            ok = app.chenyme_import_sso("sso=tok")

        self.assertTrue(ok)
        import_calls = [c for c in req_calls if c[0].endswith("/accounts/web/import")]
        self.assertEqual(len(import_calls), 1)
        _, kwargs = import_calls[0]
        self.assertNotIn("proxies", kwargs)


if __name__ == "__main__":
    unittest.main()
