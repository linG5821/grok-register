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

    def test_device_flow_happy_path(self):
        from build_sso_convert import sso_to_build
        call_log = []

        def mock_get(url, *args, **kwargs):
            call_log.append(("get", url))
            resp = MagicMock()
            resp.status = 200
            return resp

        def mock_post(url, form, **kwargs):
            call_log.append(("post", url, form))
            if "device/code" in url:
                return 200, {
                    "device_code": "D12345",
                    "user_code": "ABC-123",
                    "verification_uri_complete": "https://auth.x.ai/device?code=ABC123",
                    "interval": 5,
                    "expires_in": 1800,
                }
            if "device/verify" in url:
                raise urllib.error.HTTPError(url, 302, "Found", {"Location": "/consent"}, None)
            if "device/approve" in url:
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
        self.assertTrue(any("device/code" in str(c) for c in call_log))
        self.assertTrue(any("device/approve" in str(c) for c in call_log))


if __name__ == "__main__":
    unittest.main()
