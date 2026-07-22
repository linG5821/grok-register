import unittest
from unittest.mock import patch, MagicMock
import urllib.error


class TestSSOConvert(unittest.TestCase):
    def test_sso_expired_302_signin(self):
        from build_sso_convert import sso_to_build, SSOConvertError

        def mock_get(url, cookies, **kwargs):
            headers = {"Location": "https://accounts.x.ai/sign-in"}
            raise urllib.error.HTTPError(url, 302, "Found", headers, None)

        with patch("build_sso_convert._get_with_cookies", mock_get):
            with self.assertRaises(SSOConvertError) as cm:
                sso_to_build("dead-token")
            self.assertTrue(cm.exception.permanent)
            self.assertEqual(cm.exception.code, "sso_expired")

    def test_device_flow_happy_path_posts_cookies(self):
        """verify/approve 必须带 sso cookie，否则服务端 401。"""
        from build_sso_convert import sso_to_build

        call_log = []

        def mock_get(url, *args, **kwargs):
            call_log.append(("get", url))
            resp = MagicMock()
            resp.status = 200
            return resp

        def mock_post(url, form, **kwargs):
            call_log.append(("post", url, form, kwargs.get("cookies")))
            if "device/code" in url:
                return 200, {
                    "device_code": "D12345",
                    "user_code": "ABC-123",
                    "verification_uri_complete": "https://auth.x.ai/device?code=ABC123",
                    "interval": 1,
                    "expires_in": 60,
                }
            if "device/verify" in url:
                cookies = kwargs.get("cookies") or {}
                self.assertIn("sso", cookies)
                self.assertEqual(cookies["sso"], "valid-sso")
                return 302, ""
            if "device/approve" in url:
                cookies = kwargs.get("cookies") or {}
                self.assertIn("sso", cookies)
                self.assertEqual(cookies["sso"], "valid-sso")
                return 200, {"ok": 1}
            if "token" in url:
                return 200, {
                    "access_token": "atk_build_123",
                    "refresh_token": "rt_456",
                    "id_token": "id_jwt",
                    "expires_in": 3600,
                }
            raise NotImplementedError(f"unmocked: {url}")

        tokens = sso_to_build("valid-sso", _mock_post=mock_post, _mock_get=mock_get)
        self.assertEqual(tokens["access_token"], "atk_build_123")
        self.assertEqual(tokens["refresh_token"], "rt_456")
        # verify + approve 都必须被调用且带 cookie
        posts = [c for c in call_log if c[0] == "post"]
        self.assertTrue(any("device/verify" in str(c[1]) and c[3] and c[3].get("sso") for c in posts))
        self.assertTrue(any("device/approve" in str(c[1]) and c[3] and c[3].get("sso") for c in posts))

    def test_approve_401_is_permanent(self):
        from build_sso_convert import sso_to_build, SSOConvertError

        def mock_get(url, *args, **kwargs):
            resp = MagicMock()
            resp.status = 200
            return resp

        def mock_post(url, form, **kwargs):
            if "device/code" in url:
                return 200, {
                    "device_code": "D1",
                    "user_code": "U1",
                    "verification_uri_complete": "https://auth.x.ai/device?code=U1",
                    "interval": 1,
                    "expires_in": 60,
                }
            if "device/verify" in url:
                return 200, {}
            if "device/approve" in url:
                return 401, {"error": "unauthorized"}
            raise NotImplementedError(url)

        with self.assertRaises(SSOConvertError) as cm:
            sso_to_build("tok", _mock_post=mock_post, _mock_get=mock_get)
        self.assertTrue(cm.exception.permanent)
        self.assertEqual(cm.exception.code, "approve_unauthorized")

    def test_poll_budget_capped(self):
        """poll 上限 POLL_MAX_SEC，不能用 expires_in=1800 死等。"""
        from build_sso_convert import _poll_device_token, SSOConvertError, POLL_MAX_SEC
        import time

        calls = {"n": 0}

        def mock_post(url, form, **kwargs):
            calls["n"] += 1
            return 400, {"error": "authorization_pending"}

        t0 = time.time()
        with self.assertRaises(SSOConvertError) as cm:
            _poll_device_token(
                "dc", "cid", "https://auth.x.ai/oauth2/token",
                interval=1, expires_in=1800, post_form=mock_post,
            )
        elapsed = time.time() - t0
        self.assertEqual(cm.exception.code, "poll_timeout")
        self.assertLess(elapsed, POLL_MAX_SEC + 15)
        self.assertGreater(calls["n"], 0)

    def test_get_with_cookies_no_proxy_no_scope_bug(self):
        from build_sso_convert import _get_with_cookies

        resp = MagicMock()
        resp.status = 200
        with patch("urllib.request.urlopen", return_value=resp):
            # build_opener path when no handlers special — still works
            with patch("urllib.request.build_opener") as bo:
                opener = MagicMock()
                opener.open.return_value = resp
                bo.return_value = opener
                r = _get_with_cookies("https://accounts.x.ai/", {"sso": "x"}, proxy="")
        self.assertEqual(r.status, 200)

    def test_get_with_cookies_with_proxy(self):
        from build_sso_convert import _get_with_cookies

        resp = MagicMock()
        resp.status = 200
        opener = MagicMock()
        opener.open.return_value = resp
        with patch("urllib.request.build_opener", return_value=opener):
            r = _get_with_cookies("https://accounts.x.ai/", {"sso": "x"}, proxy="http://p:1")
        self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main()
