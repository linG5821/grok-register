"""403 号换代理复测 + 官方 refresh 的核心周期逻辑。"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import build_liveness as bl

try:
    from build_token_refresh import TokenRefreshError, refresh_access_token
except Exception:  # pragma: no cover
    TokenRefreshError = RuntimeError  # type: ignore
    refresh_access_token = None  # type: ignore


def load_emails_file(path: str) -> list[str]:
    """读取邮箱列表：去空行、# 注释、小写去重保序。"""
    emails: list[str] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            # 支持 email,password 等首列
            if "," in text:
                text = text.split(",", 1)[0].strip()
            email = text.lower()
            if not email or "@" not in email:
                continue
            if email in seen:
                continue
            seen.add(email)
            emails.append(email)
    return emails


def bot_flag_from_token(access_token: str) -> Any:
    claims = bl._decode_jwt_payload(access_token)
    return claims.get("bot_flag_source")


def index_build_accounts(exported: list) -> dict[str, dict]:
    """export accounts → email → {access_token, refresh_token, bot_flag, raw}。"""
    index: dict[str, dict] = {}
    for account in exported or []:
        if not isinstance(account, dict):
            continue
        provider = str(account.get("provider") or "").strip().lower()
        if provider != "grok_build":
            continue
        email = str(account.get("name") or account.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        access = str(
            account.get("access_token")
            or account.get("accessToken")
            or ""
        ).strip()
        refresh = str(
            account.get("refresh_token")
            or account.get("refreshToken")
            or ""
        ).strip()
        index[email] = {
            "access_token": access,
            "refresh_token": refresh,
            "bot_flag": bot_flag_from_token(access) if access else None,
            "raw": account,
        }
    return index


def _pick_proxies(
    max_proxies: int,
    exclude: set[str],
    extra_proxies: Optional[list[str]] = None,
    list_candidates: Optional[Callable[..., list[str]]] = None,
) -> list[str]:
    out: list[str] = []
    blocked = set(exclude)
    if extra_proxies:
        for raw in extra_proxies:
            text = str(raw or "").strip()
            if text and text not in blocked and text not in out:
                out.append(text)
                if len(out) >= max_proxies:
                    return out
    picker = list_candidates
    if picker is None:
        try:
            from proxy_manager import list_proxy_candidates

            picker = list_proxy_candidates
        except Exception:
            picker = lambda limit=5, exclude=None: []
    for url in picker(limit=max_proxies, exclude=blocked | set(out)):
        text = str(url or "").strip()
        if text and text not in blocked and text not in out:
            out.append(text)
        if len(out) >= max_proxies:
            break
    return out[:max_proxies]


def _run_proxy_phase(
    access_token: str,
    *,
    phase: int,
    max_proxies: int,
    tried: set[str],
    extra_proxies: Optional[list[str]],
    config: Optional[dict],
    profile: Optional[dict],
    email: str,
    bot_flag: Any,
    http_post: Optional[Callable[..., Any]],
    log: Optional[Callable[[str], None]],
    list_candidates: Optional[Callable[..., list[str]]] = None,
) -> tuple[Optional[dict], list[dict]]:
    """返回 (live_result_or_None, attempts)。"""
    attempts: list[dict] = []
    proxies = _pick_proxies(max_proxies, tried, extra_proxies, list_candidates)
    if not proxies and log:
        log(f"[!] {email} phase{phase}: 无可用代理候选")
    for proxy in proxies:
        tried.add(proxy)
        if log:
            log(f"[*] {email} phase{phase} probe proxy={proxy[:48]}…")
        result = bl.probe_build_responses(
            access_token,
            proxy=proxy,
            config=config,
            profile=profile,
            email=email,
            bot_flag=bot_flag,
            http_post=http_post,
            output_path="",  # 不写注册 liveness 文件
            log_callback=None,
        )
        attempt = {
            "phase": phase,
            "proxy": proxy,
            "http_code": result.get("http_code"),
            "status": result.get("status"),
            "error": result.get("error") or "",
            "preview": result.get("preview") or "",
        }
        attempts.append(attempt)
        if result.get("ok") and result.get("status") == "live":
            return result, attempts
    return None, attempts


def run_account_cycle(
    email: str,
    creds: Optional[dict],
    *,
    max_proxies: int = 5,
    enable_refresh: bool = True,
    extra_proxies: Optional[list[str]] = None,
    config: Optional[dict] = None,
    profile: Optional[dict] = None,
    http_post: Optional[Callable[..., Any]] = None,
    refresh_fn: Optional[Callable[..., dict]] = None,
    list_candidates: Optional[Callable[..., list[str]]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    dry_run: bool = False,
) -> dict:
    """单号完整周期：phase1 代理 → refresh → phase2 代理。"""
    email_n = str(email or "").strip().lower()
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "email": email_n,
        "final_status": "error",
        "bot_flag": None,
        "refreshed": False,
        "proxies_tried": [],
        "attempts": [],
        "live_proxy": "",
        "client_version": (profile or {}).get("client_version", ""),
        "model": str((config or {}).get("build_liveness_model") or bl.DEFAULT_MODEL),
        "error": "",
    }
    log = log_callback

    if not creds or not str(creds.get("access_token") or "").strip():
        record["final_status"] = "skipped_no_token"
        record["error"] = "no_build_token"
        if log:
            log(f"[Debug] {email_n}: skipped_no_token")
        return record

    access = str(creds.get("access_token") or "").strip()
    refresh = str(creds.get("refresh_token") or "").strip()
    bot_flag = creds.get("bot_flag")
    if bot_flag is None:
        bot_flag = bot_flag_from_token(access)
    record["bot_flag"] = bot_flag

    if dry_run:
        proxies = _pick_proxies(max_proxies, set(), extra_proxies, list_candidates)
        record["final_status"] = "dry_run"
        record["proxies_tried"] = proxies
        record["error"] = f"dry_run candidates={len(proxies)} refresh={'yes' if refresh else 'no'}"
        return record

    tried: set[str] = set()
    live, attempts = _run_proxy_phase(
        access,
        phase=1,
        max_proxies=max_proxies,
        tried=tried,
        extra_proxies=extra_proxies,
        config=config,
        profile=profile,
        email=email_n,
        bot_flag=bot_flag,
        http_post=http_post,
        log=log,
        list_candidates=list_candidates,
    )
    record["attempts"].extend(attempts)
    record["proxies_tried"] = list(tried)
    if live:
        record["final_status"] = "live_proxy"
        record["live_proxy"] = str(live.get("proxy") or "")
        record["client_version"] = live.get("client_version") or record["client_version"]
        if log:
            log(f"[+] {email_n}: live_proxy via {record['live_proxy'][:48]}")
        return record

    if not enable_refresh:
        record["final_status"] = _finalize_dead_or_error(record["attempts"], bot_flag)
        record["error"] = "phase1_failed_no_refresh"
        return record

    if not refresh:
        record["final_status"] = _finalize_dead_or_error(record["attempts"], bot_flag)
        record["error"] = "no_refresh_token"
        if log:
            log(f"[!] {email_n}: no_refresh_token")
        return record

    do_refresh = refresh_fn or refresh_access_token
    if do_refresh is None:
        record["final_status"] = "error"
        record["error"] = "refresh_unavailable"
        return record

    refresh_proxy = ""
    if record["attempts"]:
        refresh_proxy = str(record["attempts"][-1].get("proxy") or "")
    if not refresh_proxy and tried:
        refresh_proxy = next(iter(tried))

    try:
        if log:
            log(f"[*] {email_n}: official OAuth refresh…")
        tokens = do_refresh(refresh, proxy=refresh_proxy, timeout=30.0)
        access = str(tokens.get("access_token") or "").strip()
        if tokens.get("refresh_token"):
            refresh = str(tokens.get("refresh_token") or refresh).strip()
        record["refreshed"] = True
        bot_flag = bot_flag_from_token(access)
        record["bot_flag"] = bot_flag
        record["attempts"].append(
            {
                "phase": "refresh",
                "proxy": refresh_proxy,
                "http_code": 200,
                "status": "refreshed",
                "error": "",
                "preview": "",
            }
        )
    except Exception as exc:
        permanent = bool(getattr(exc, "permanent", False))
        record["attempts"].append(
            {
                "phase": "refresh",
                "proxy": refresh_proxy,
                "http_code": getattr(exc, "status", None),
                "status": "refresh_failed",
                "error": str(exc)[:300],
                "preview": "",
            }
        )
        record["final_status"] = "dead" if permanent else "error"
        record["error"] = f"refresh_failed:{exc}"[:300]
        record["proxies_tried"] = list(tried)
        if log:
            log(f"[!] {email_n}: refresh failed: {exc}")
        return record

    if not access:
        record["final_status"] = "error"
        record["error"] = "refresh_empty_access"
        return record

    live2, attempts2 = _run_proxy_phase(
        access,
        phase=2,
        max_proxies=max_proxies,
        tried=tried,
        extra_proxies=extra_proxies,
        config=config,
        profile=profile,
        email=email_n,
        bot_flag=bot_flag,
        http_post=http_post,
        log=log,
        list_candidates=list_candidates,
    )
    record["attempts"].extend(attempts2)
    record["proxies_tried"] = list(tried)
    if live2:
        record["final_status"] = "live_refresh"
        record["live_proxy"] = str(live2.get("proxy") or "")
        record["client_version"] = live2.get("client_version") or record["client_version"]
        if log:
            log(f"[+] {email_n}: live_refresh via {record['live_proxy'][:48]}")
        return record

    record["final_status"] = _finalize_dead_or_error(record["attempts"], bot_flag)
    if not record["error"]:
        record["error"] = "all_phases_failed"
    if log:
        log(f"[!] {email_n}: {record['final_status']} {record['error']}")
    return record


def _finalize_dead_or_error(attempts: list[dict], bot_flag: Any) -> str:
    statuses = [str(a.get("status") or "") for a in attempts if a.get("phase") in (1, 2)]
    codes = [a.get("http_code") for a in attempts if a.get("phase") in (1, 2)]
    dead_hits = sum(1 for s in statuses if s == "dead")
    if bot_flag is not None or dead_hits >= 2 or (dead_hits >= 1 and all(c == 403 for c in codes if c)):
        return "dead"
    if any(s == "error" for s in statuses) and dead_hits == 0:
        return "error"
    return "dead"


def summarize_results(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("final_status") or "error")
        counts[key] = counts.get(key, 0) + 1
    counts["total"] = len(rows)
    return counts
