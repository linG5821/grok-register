"""chenyme sso_build.go 复刻：SSO cookie -> Device Flow -> Build OAuth token。"""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from cpa_xai.oauth_device import CLIENT_ID, _post_form


SSO_SCOPE = " ".join([
    "openid", "profile", "email", "offline_access",
    "grok-cli:access", "api:access",
    "conversations:read", "conversations:write",
])
ACCOUNTS_URL = "https://accounts.x.ai/"
DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
TOKEN_URL = "https://auth.x.ai/oauth2/token"


class SSOConvertError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False, code: str = "", http_status: int = 0):
        super().__init__(message)
        self.permanent = bool(permanent)
        self.code = str(code or "")
        self.http_status = int(http_status or 0)


def _get_with_cookies(url: str, cookies: dict[str, str], proxy: str = "", timeout: float = 30) -> Any:
    """带 cookie 的 GET，返回 response 对象或抛异常。"""
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    request = urllib.request.Request(url, headers={"Cookie": cookie_str, "User-Agent": "grok-register-sso/1.0"})
    if proxy:
        import urllib.parse
        from urllib.request import ProxyHandler, build_opener

        proxy_type = urllib.parse.urlparse(proxy).scheme or "http"
        opener = build_opener(ProxyHandler({proxy_type: proxy}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def _poll_device_token(
    device_code: str,
    client_id: str,
    token_url: str,
    interval: int,
    expires_in: int,
    proxy: str = "",
    log: Optional[Callable[[str], None]] = None,
    post_form: Optional[Callable[..., Any]] = None,
) -> dict:
    import time

    deadline = time.time() + max(int(expires_in) - 5, 30)
    sleep_sec = max(int(interval), 1)
    do_post = post_form or _post_form
    while time.time() < deadline:
        try:
            status, payload = do_post(
                token_url,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": str(device_code).strip(),
                    "client_id": client_id,
                },
                timeout=30,
                proxy=proxy,
                retries=0,
            )
            if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
                return {
                    "access_token": str(payload.get("access_token") or "").strip(),
                    "refresh_token": str(payload.get("refresh_token") or "").strip(),
                    "id_token": str(payload.get("id_token") or "").strip(),
                    "expires_in": int(payload.get("expires_in") or 0),
                    "raw": payload,
                }
            error = str(payload.get("error") or "").lower() if isinstance(payload, dict) else ""
            if error in ("authorization_pending", "slow_down"):
                time.sleep(sleep_sec if error == "authorization_pending" else sleep_sec + 5)
                continue
            if error in ("access_denied", "expired_token", "invalid_grant"):
                raise SSOConvertError(f"device token rejected: {error}", permanent=True, code=error, http_status=status)
            raise SSOConvertError(f"device token poll failed HTTP {status}", code=f"poll_http_{status}", http_status=status)
        except SSOConvertError:
            raise
        except Exception as exc:
            if log:
                log(f"poll retry: {exc}")
            time.sleep(sleep_sec)
    raise SSOConvertError("device flow poll timeout", permanent=False, code="poll_timeout")


def sso_to_build(
    sso_token: str,
    proxy: str = "",
    timeout: float = 90,
    log: Optional[Callable[[str], None]] = None,
    *,
    _mock_get: Optional[Callable[..., Any]] = None,
    _mock_post: Optional[Callable[..., Any]] = None,
) -> dict:
    sso = str(sso_token or "").strip()
    if not sso:
        raise SSOConvertError("missing sso token", permanent=True, code="missing_sso")
    if sso.lower().startswith("sso="):
        sso = sso[4:].strip()

    cookies = {"sso": sso, "sso-rw": sso}
    do_get = _mock_get or _get_with_cookies
    do_post = _mock_post or _post_form

    # Step 1: 校验 SSO 是否有效
    try:
        if log:
            log(f"[SSO] 1/6 verify accounts.x.ai {proxy}")
        _ = do_get(ACCOUNTS_URL, cookies, proxy=proxy, timeout=timeout)
    except urllib.error.HTTPError as exc:
        location = str(exc.headers.get("Location") or "").lower()
        if exc.code in (302, 303) and "sign-in" in location:
            raise SSOConvertError("sso expired: 302 to sign-in", permanent=True, code="sso_expired", http_status=exc.code)
        raise SSOConvertError(f"accounts.x.ai HTTP {exc.code}", code=f"accounts_http_{exc.code}", http_status=exc.code)
    except Exception as exc:
        raise SSOConvertError(f"accounts.x.ai failed: {exc}", code="accounts_net_error") from exc

    # Step 2: Request device code
    try:
        if log:
            log("[SSO] 2/6 request device code")
        status, payload = do_post(
            DEVICE_CODE_URL,
            {"client_id": CLIENT_ID, "scope": SSO_SCOPE},
            proxy=proxy,
            timeout=30,
            retries=1,
        )
    except Exception as exc:
        raise SSOConvertError(f"device code request failed: {exc}", code="device_code_request_failed") from exc
    if status != 200 or not isinstance(payload, dict):
        raise SSOConvertError(f"device code HTTP {status}", code=f"device_code_http_{status}", http_status=status)

    device_code = str(payload.get("device_code") or "").strip()
    user_code = str(payload.get("user_code") or "").strip()
    verify_uri = str(payload.get("verification_uri_complete") or "").strip()
    interval = max(int(payload.get("interval") or 5), 1)
    expires_in = max(int(payload.get("expires_in") or 1800), 60)
    if not device_code or not user_code:
        raise SSOConvertError("device code response missing fields", permanent=False, code="device_code_incomplete")

    # Step 3: GET verification_uri_complete
    try:
        if log:
            log("[SSO] 3/6 open verification uri")
        _ = do_get(verify_uri, cookies, proxy=proxy, timeout=30)
    except Exception as exc:
        raise SSOConvertError(f"open verification uri failed: {exc}", code="verify_uri_failed") from exc

    # Step 4: POST device/verify
    try:
        if log:
            log("[SSO] 4/6 device verify")
        status, _ = do_post(VERIFY_URL, {"user_code": user_code}, proxy=proxy, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code not in (302, 303):
            raise SSOConvertError(f"device verify HTTP {exc.code}", code=f"verify_http_{exc.code}", http_status=exc.code) from exc
    except Exception as exc:
        raise SSOConvertError(f"device verify failed: {exc}", code="verify_failed") from exc

    # Step 5: POST device/approve
    try:
        if log:
            log("[SSO] 5/6 device approve")
        status, _ = do_post(
            APPROVE_URL,
            {"user_code": user_code, "action": "allow", "principal_type": "User", "principal_id": ""},
            proxy=proxy,
            timeout=30,
        )
        if status not in (200, 204, 302, 303):
            raise SSOConvertError(f"device approve HTTP {status}", code=f"approve_http_{status}", http_status=status)
    except Exception as exc:
        raise SSOConvertError(f"device approve failed: {exc}", code="approve_failed") from exc

    # Step 6: Poll token
    if log:
        log("[SSO] 6/6 poll token")
    return _poll_device_token(device_code, CLIENT_ID, TOKEN_URL, interval, expires_in, proxy=proxy, log=log, post_form=do_post)
