"""官方 xAI OAuth refresh_token 交换（Build CLI client_id）。"""
from __future__ import annotations

from typing import Any, Optional

from cpa_xai.oauth_device import CLIENT_ID, _post_form

DEFAULT_TOKEN_URL = "https://auth.x.ai/oauth2/token"

# invalid_grant / 明确鉴权失败视为永久
_PERMANENT_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
        "access_denied",
        "expired_token",
    }
)


class TokenRefreshError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False, status: int = 0, code: str = ""):
        super().__init__(message)
        self.permanent = bool(permanent)
        self.status = int(status or 0)
        self.code = str(code or "")


def refresh_access_token(
    refresh_token: str,
    *,
    client_id: str = CLIENT_ID,
    token_url: str = DEFAULT_TOKEN_URL,
    proxy: str = "",
    timeout: float = 30.0,
    post_form=None,
) -> dict:
    """用 refresh_token 换 access_token。

    返回::
        {
          "access_token": str,
          "refresh_token": str,  # 若上游未返回则回落旧值
          "expires_in": int,
          "id_token": str,
          "raw": dict,
        }
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise TokenRefreshError("missing_refresh_token", permanent=True, code="missing_refresh_token")

    form = {
        "grant_type": "refresh_token",
        "client_id": str(client_id or CLIENT_ID).strip() or CLIENT_ID,
        "refresh_token": rt,
    }
    do_post = post_form or _post_form
    proxy_url = str(proxy or "").strip() or None
    status, payload = do_post(
        str(token_url or DEFAULT_TOKEN_URL).strip() or DEFAULT_TOKEN_URL,
        form,
        timeout=float(timeout),
        proxy=proxy_url,
        retries=1,
        retry_sleep=1.0,
    )

    if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
        access = str(payload.get("access_token") or "").strip()
        new_refresh = str(payload.get("refresh_token") or "").strip() or rt
        return {
            "access_token": access,
            "refresh_token": new_refresh,
            "expires_in": int(payload.get("expires_in") or 0),
            "id_token": str(payload.get("id_token") or "").strip(),
            "raw": payload,
        }

    error_code = ""
    error_description = ""
    if isinstance(payload, dict):
        error_code = str(payload.get("error") or "").strip()
        error_description = str(payload.get("error_description") or "").strip()
    permanent = error_code in _PERMANENT_ERRORS or status in (400, 401)
    msg = f"refresh failed HTTP {status}"
    if error_code:
        msg += f": {error_code}"
    if error_description:
        msg += f" ({error_description})"
    raise TokenRefreshError(msg, permanent=permanent, status=status, code=error_code or f"http_{status}")
