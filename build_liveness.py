"""Build 真活检测：按 grok2api CLI 协议 POST /v1/responses 发 hi。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_CLIENT_VERSION = "0.2.103"
DEFAULT_CLIENT_IDENTIFIER = "grok-shell"
DEFAULT_TOKEN_AUTH = "xai-grok-cli"
DEFAULT_MODEL = "grok-4.5"
DEFAULT_PROMPT = "hi"

_agent_id = str(uuid.uuid4())
_profile_lock = threading.Lock()
_cached_profile: Optional[dict] = None
_cached_profile_at = 0.0
_liveness_path = ""
_liveness_path_lock = threading.Lock()


def default_user_agent(version: str = DEFAULT_CLIENT_VERSION) -> str:
    ver = str(version or DEFAULT_CLIENT_VERSION).strip() or DEFAULT_CLIENT_VERSION
    return f"grok-shell/{ver} (linux; x86_64)"


def resolve_cli_profile(
    config: Optional[dict] = None,
    settings_payload: Optional[dict] = None,
) -> dict:
    """合并 CLI 参数：recommended > providerBuild > config > 硬编码默认。"""
    cfg = dict(config or {})
    recommended: dict = {}
    provider: dict = {}
    if isinstance(settings_payload, dict):
        rec = settings_payload.get("recommendedProviderBuild")
        if isinstance(rec, dict):
            recommended = rec
        conf = settings_payload.get("config")
        if isinstance(conf, dict):
            pb = conf.get("providerBuild")
            if isinstance(pb, dict):
                provider = pb
        elif isinstance(settings_payload.get("providerBuild"), dict):
            provider = settings_payload["providerBuild"]

    def _pick(*values, fallback=""):
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return fallback

    client_version = _pick(
        recommended.get("clientVersion"),
        provider.get("clientVersion"),
        cfg.get("build_liveness_client_version"),
        fallback=DEFAULT_CLIENT_VERSION,
    )
    user_agent = _pick(
        recommended.get("userAgent"),
        provider.get("userAgent"),
        cfg.get("build_liveness_user_agent"),
        fallback=default_user_agent(client_version),
    )
    client_identifier = _pick(
        provider.get("clientIdentifier"),
        cfg.get("build_liveness_client_identifier"),
        fallback=DEFAULT_CLIENT_IDENTIFIER,
    )
    token_auth = _pick(
        provider.get("tokenAuth"),
        cfg.get("build_liveness_token_auth"),
        fallback=DEFAULT_TOKEN_AUTH,
    )
    base_url = _pick(
        cfg.get("build_liveness_base_url"),
        provider.get("baseURL") or provider.get("baseUrl"),
        fallback=DEFAULT_BASE_URL,
    ).rstrip("/")
    return {
        "client_version": client_version,
        "user_agent": user_agent,
        "client_identifier": client_identifier,
        "token_auth": token_auth,
        "base_url": base_url,
    }


def get_cached_cli_profile(
    config: Optional[dict] = None,
    fetch_settings: Optional[Callable[[], Optional[dict]]] = None,
    now: Optional[float] = None,
) -> dict:
    """进程内缓存 profile；TTL 到期或无缓存时可选拉取 chenyme settings。"""
    global _cached_profile, _cached_profile_at
    cfg = dict(config or {})
    ttl = int(cfg.get("build_liveness_cli_cache_ttl_sec") or 3600)
    stamp = time.time() if now is None else float(now)
    with _profile_lock:
        if _cached_profile and (stamp - _cached_profile_at) < max(ttl, 1):
            return dict(_cached_profile)
    settings = None
    if bool(cfg.get("build_liveness_fetch_cli_from_chenyme", True)) and fetch_settings:
        try:
            settings = fetch_settings()
        except Exception:
            settings = None
    profile = resolve_cli_profile(cfg, settings)
    with _profile_lock:
        _cached_profile = dict(profile)
        _cached_profile_at = stamp
    return dict(profile)


def clear_cli_profile_cache():
    global _cached_profile, _cached_profile_at
    with _profile_lock:
        _cached_profile = None
        _cached_profile_at = 0.0


def build_cli_headers(
    access_token: str,
    profile: dict,
    model: str = "",
    user_id: str = "",
    agent_id: str = "",
) -> dict:
    """对齐 grok2api cli.Adapter.applyHeaders（trace 模式）。"""
    token = str(access_token or "").strip()
    session_id = str(uuid.uuid4())
    request_id = str(uuid.uuid4())
    aid = str(agent_id or _agent_id).strip() or _agent_id
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": str(profile.get("token_auth") or DEFAULT_TOKEN_AUTH),
        "x-grok-client-version": str(profile.get("client_version") or DEFAULT_CLIENT_VERSION),
        "x-grok-client-identifier": str(profile.get("client_identifier") or DEFAULT_CLIENT_IDENTIFIER),
        "x-grok-client-mode": "headless",
        "User-Agent": str(profile.get("user_agent") or default_user_agent()),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-authenticateresponse": "authenticate-response",
        "x-grok-agent-id": aid,
        "x-grok-session-id": session_id,
        "x-grok-conv-id": session_id,
        "x-grok-req-id": request_id,
        "traceparent": f"00-{uuid.uuid4().hex}-{uuid.uuid4().hex[:16]}-01",
    }
    model_name = str(model or "").strip()
    if model_name:
        headers["x-grok-model-override"] = model_name
    uid = str(user_id or "").strip()
    if uid:
        headers["x-grok-user-id"] = uid
    return headers


def extract_output_text(payload: Any) -> str:
    """从 /responses JSON 抽出首段非空模型文本。"""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" or "content" in item:
                content = item.get("content")
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        for key in ("output_text", "text"):
                            value = part.get(key)
                            if isinstance(value, str) and value.strip():
                                return value.strip()
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    # 浅层启发式
    for key in ("text", "message", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = extract_output_text(value)
            if nested:
                return nested
    return ""


def classify_liveness(
    http_code: Optional[int],
    body_text: str = "",
    output_text: str = "",
    error: str = "",
) -> str:
    """返回 live | dead | error。"""
    if error and http_code is None:
        return "error"
    code = int(http_code or 0)
    body_lower = str(body_text or "").lower()
    if code == 403 or "access denied" in body_lower:
        return "dead"
    if 200 <= code < 300 and str(output_text or "").strip():
        return "live"
    if 200 <= code < 300:
        return "error"
    if code >= 500 or code == 0:
        return "error"
    return "error"


def preview_text(text: str, limit: int = 200) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def set_liveness_output_path(path: str):
    global _liveness_path
    with _liveness_path_lock:
        _liveness_path = str(path or "").strip()


def get_liveness_output_path() -> str:
    with _liveness_path_lock:
        return _liveness_path


def liveness_path_for_accounts(accounts_output_file: str) -> str:
    """accounts_YYYYMMDD_HHMMSS.txt → 同目录 liveness_YYYYMMDD_HHMMSS.jsonl。"""
    path = str(accounts_output_file or "").strip()
    if not path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.abspath(f"liveness_{stamp}.jsonl")
    directory = os.path.dirname(os.path.abspath(path)) or os.getcwd()
    base = os.path.basename(path)
    match = re.search(r"(\d{8}_\d{6})", base)
    stamp = match.group(1) if match else datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(directory, f"liveness_{stamp}.jsonl")


def append_liveness_jsonl(path: str, record: dict):
    target = str(path or "").strip()
    if not target:
        return
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line)


def user_id_from_token(access_token: str) -> str:
    claims = _decode_jwt_payload(access_token)
    for key in ("sub", "principal_id", "user_id", "uid"):
        value = claims.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _decode_jwt_payload(token: str) -> dict:
    import base64

    text = str(token or "").strip()
    if not text or text.count(".") < 2:
        return {}
    try:
        segment = text.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment)
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        return {}


def probe_build_responses(
    access_token: str,
    *,
    proxy: str = "",
    config: Optional[dict] = None,
    profile: Optional[dict] = None,
    model: str = "",
    prompt: str = "",
    timeout: Optional[float] = None,
    http_post: Optional[Callable[..., Any]] = None,
    email: str = "",
    bot_flag: Any = None,
    output_path: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """对 Build token 发一次 /responses；不抛异常。"""
    cfg = dict(config or {})
    result = {
        "ok": False,
        "status": "error",
        "http_code": None,
        "proxy": str(proxy or "").strip(),
        "model": str(model or cfg.get("build_liveness_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        "preview": "",
        "error": "",
        "bot_flag": bot_flag,
        "client_version": "",
        "base_url": "",
        "email": str(email or "").strip().lower(),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    token = str(access_token or "").strip()
    if not token:
        result["status"] = "error"
        result["error"] = "no_build_token"
        _finish_probe(result, output_path, log_callback)
        return result

    prof = dict(profile or resolve_cli_profile(cfg))
    result["client_version"] = prof.get("client_version", "")
    result["base_url"] = str(prof.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    model_name = result["model"]
    prompt_text = str(prompt or cfg.get("build_liveness_prompt") or DEFAULT_PROMPT).strip() or DEFAULT_PROMPT
    timeout_sec = float(timeout if timeout is not None else cfg.get("build_liveness_timeout_sec") or 60)
    url = f"{result['base_url']}/responses"
    headers = build_cli_headers(
        token,
        prof,
        model=model_name,
        user_id=user_id_from_token(token),
    )
    body = {"model": model_name, "input": prompt_text, "stream": False}
    post = http_post
    if post is None:
        from browser_runtime import http_post as _http_post

        post = _http_post

    proxies = None
    force_direct = True
    proxy_url = str(proxy or "").strip()
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
        force_direct = False

    try:
        resp = post(
            url,
            headers=headers,
            json=body,
            timeout=timeout_sec,
            proxies=proxies,
            force_direct=force_direct,
        )
        code = int(getattr(resp, "status_code", 0) or 0)
        result["http_code"] = code
        raw_text = ""
        try:
            raw_text = getattr(resp, "text", "") or ""
        except Exception:
            raw_text = ""
        payload = None
        try:
            if hasattr(resp, "json"):
                payload = resp.json()
        except Exception:
            payload = None
        if payload is None and raw_text:
            try:
                payload = json.loads(raw_text)
            except Exception:
                payload = None
        text = extract_output_text(payload) if payload is not None else ""
        result["preview"] = preview_text(text)
        result["status"] = classify_liveness(code, raw_text, text)
        result["ok"] = result["status"] == "live"
        if not result["ok"] and not result["error"]:
            if result["status"] == "dead":
                result["error"] = f"http_{code}_access_denied"
            elif 200 <= code < 300:
                result["error"] = "empty_output"
            else:
                result["error"] = f"http_{code}"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)[:300]
        result["ok"] = False

    _finish_probe(result, output_path, log_callback)
    return result


def _finish_probe(result: dict, output_path: str, log_callback: Optional[Callable[[str], None]]):
    path = str(output_path or "").strip() or get_liveness_output_path()
    if path:
        try:
            append_liveness_jsonl(path, result)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 liveness 文件失败: {exc}")
    if not log_callback:
        return
    email = result.get("email") or "?"
    status = result.get("status")
    if status == "live":
        log_callback(
            f"[+] Build 真活 {email} model={result.get('model')} preview={result.get('preview')!r}"
        )
    elif status == "dead":
        log_callback(
            f"[!] Build 不可用 {email} http={result.get('http_code')} {result.get('error')}"
        )
    elif status == "skipped":
        log_callback(f"[Debug] Build 真活跳过 {email}: {result.get('error')}")
    else:
        log_callback(f"[!] Build 真活异常 {email}: {result.get('error')}")
