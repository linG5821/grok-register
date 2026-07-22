#!/usr/bin/env python3
"""SSO Device Flow 转 Build 测活 + 浏览器重抽 SSO。

不指定 --emails 时默认处理 chenyme export 全量 grok_web 号。

用法::

    # 处理 emails.txt（仅 Device Flow，SSO 过期只记死，不重抽）
    python scripts/reprobe_build_liveness.py --emails emails.txt

    # 全量 export 中 grok_web 号，SSO 过期时浏览器重抽（需要 accounts_*.txt 里密码）
    python scripts/reprobe_build_liveness.py --enable-relogin

    # dry-run：只匹配凭据/列代理
    python scripts/reprobe_build_liveness.py --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from curl_cffi import requests

import build_liveness as bl
import build_reprobe as reprobe
from app_config import load_config
from build_token_refresh import refresh_access_token


def _log(msg: str) -> None:
    print(msg, flush=True)


def login_chenyme(base: str, username: str, password: str) -> str:
    endpoint = f"{base.rstrip('/')}/api/admin/v1/auth/login"
    resp = requests.post(
        endpoint,
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    token = (
        (payload.get("data") or {})
        .get("tokens", {})
        .get("accessToken", "")
    )
    if not token:
        raise RuntimeError("chenyme 登录响应缺少 accessToken")
    return str(token)


def export_accounts(base: str, admin_token: str) -> list:
    endpoint = f"{base.rstrip('/')}/api/admin/v1/accounts/export"
    resp = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=120,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    accounts = payload.get("accounts") or []
    return accounts if isinstance(accounts, list) else []


def index_sso_accounts(exported: list) -> dict[str, str]:
    """export 中 grok_web -> email -> sso_access_token。"""

    index: dict[str, str] = {}
    for account in exported or []:
        if not isinstance(account, dict):
            continue
        provider = str(account.get("provider") or "").strip().lower()
        if provider != "grok_web":
            continue
        email = str(account.get("name") or account.get("email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        access = str(account.get("access_token") or account.get("accessToken") or "").strip()
        if access:
            index[email] = access
    return index


def load_local_accounts_files() -> tuple[dict[str, str], dict[str, str]]:
    """加载当前目录所有 accounts_*.txt，返回 email->sso 和 email->password。"""

    sso_by_email: dict[str, str] = {}
    pass_by_email: dict[str, str] = {}
    files = glob.glob(os.path.join(ROOT, "accounts_*.txt"))
    for path in sorted(files):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    parts = raw.rstrip(chr(10)).split("----", 2)
                    if len(parts) < 3:
                        continue
                    email = parts[0].strip().lower()
                    password = parts[1].strip()
                    sso = parts[2].strip()
                    if not email or "@" not in email:
                        continue
                    if sso and email not in sso_by_email:
                        sso_by_email[email] = sso
                    if password and email not in pass_by_email:
                        pass_by_email[email] = password
        except Exception as exc:
            _log(f"[Debug] 读取 accounts 文件失败 {path}: {exc}")
    return sso_by_email, pass_by_email


def load_proxy_file(path: str) -> list[str]:
    if not path:
        return []
    out: list[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            out.append(text)
    return out


def fetch_cli_profile(cfg: dict, base: str, admin_token: str) -> dict:
    if not cfg.get("build_liveness_fetch_cli_from_chenyme", True):
        return bl.resolve_cli_profile(cfg)

    def fetch():
        endpoint = f"{base.rstrip('/')}/api/admin/v1/settings"
        resp = requests.get(
            endpoint,
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        payload = resp.json() if hasattr(resp, "json") else None
        return payload if isinstance(payload, dict) else None

    return bl.get_cached_cli_profile(cfg, fetch_settings=fetch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SSO Device Flow Build 复测 + SSO 重抽")
    parser.add_argument("--emails", default="", help="邮箱列表文件，每行一个；不指定则全量")
    parser.add_argument("--max-proxies", type=int, default=5, help="每号最多换代理数（默认 5）")
    parser.add_argument("--model", default="", help="覆盖 build_liveness_model")
    parser.add_argument("--no-refresh", action="store_true", help="跳过 Build refresh 路")
    parser.add_argument("--enable-relogin", action="store_true", help="SSO 过期时浏览器重抽")
    parser.add_argument("--dry-run", action="store_true", help="只匹配凭据/列代理，不探测")
    parser.add_argument("--proxy-file", default="", help="额外代理 URL 列表")
    parser.add_argument("--out-dir", default="", help="结果目录，默认项目根")
    parser.add_argument("--config", default="", help="config.json 路径")
    args = parser.parse_args(argv)

    config_path = str(args.config or "").strip()
    if config_path:
        os.environ.setdefault("GROK_REGISTER_CONFIG", config_path)
    cfg = load_config()
    if args.model:
        cfg["build_liveness_model"] = str(args.model).strip()

    base = str(cfg.get("chenyme_grok2api_base") or "").strip().rstrip("/")
    username = str(cfg.get("chenyme_grok2api_username") or "").strip()
    password = str(cfg.get("chenyme_grok2api_password") or "").strip()
    if not base or not username or not password:
        print("请在 config.json 配置 chenyme_grok2api_base/username/password", file=sys.stderr)
        return 2

    extra_proxies = load_proxy_file(str(args.proxy_file or "").strip())

    try:
        from proxy_manager import start_background_scan, proxy_health_should_run

        if proxy_health_should_run() and not args.dry_run:
            _log("[*] 启动代理健康扫描（后台）…")
            start_background_scan(log=_log)
            time.sleep(1.0)
    except Exception as exc:
        _log(f"[Debug] 健康扫描未启动: {exc}")

    _log(f"[*] 登录 chenyme {base}")
    try:
        admin_token = login_chenyme(base, username, password)
    except Exception as exc:
        print(f"登录失败: {exc}", file=sys.stderr)
        return 2

    _log("[*] 全量 export 账号…")
    try:
        exported = export_accounts(base, admin_token)
    except Exception as exc:
        print(f"export 失败: {exc}", file=sys.stderr)
        return 2

    build_index = reprobe.index_build_accounts(exported)
    sso_index = index_sso_accounts(exported)
    local_sso, local_pass = load_local_accounts_files()

    # chenyme 优先，本地补全
    merged_sso: dict[str, str] = dict(local_sso)
    merged_sso.update(sso_index)
    merged_pass: dict[str, str] = dict(local_pass)

    # 确定目标列表
    emails: list[str] = []
    emails_path = str(args.emails or "").strip()
    if emails_path:
        if not os.path.isfile(emails_path):
            print(f"邮箱文件不存在: {emails_path}", file=sys.stderr)
            return 2
        emails = reprobe.load_emails_file(emails_path)
        if not emails:
            print("邮箱列表为空", file=sys.stderr)
            return 2
    else:
        emails = list(sorted(set(merged_sso.keys()) | set(build_index.keys())))
        emails = [e for e in emails if "@" in e]

    if not emails:
        print("没有目标邮箱", file=sys.stderr)
        return 2

    _log(f"[*] 目标邮箱 {len(emails)} 个；SSO: {len(merged_sso)} Build: {len(build_index)}")

    profile = fetch_cli_profile(cfg, base, admin_token)
    _log(
        f"[*] CLI profile version={profile.get('client_version')} "
        f"ua={str(profile.get('user_agent') or '')[:40]}"
    )

    out_dir = str(args.out_dir or "").strip() or ROOT
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"reprobe_sso_{stamp}.jsonl")
    import_path = os.path.join(out_dir, f"reprobe_build_import_{stamp}.json")

    from build_liveness import _decode_jwt_payload
    import_accounts: list[dict] = []

    rows: list[dict[str, Any]] = []
    for i, email in enumerate(emails, 1):
        _log(f"--- [{i}/{len(emails)}] {email} ---")
        sso = merged_sso.get(email, "")
        passwd = merged_pass.get(email, "")
        creds = build_index.get(email)

        # 优先 SSO probe 路
        if sso:
            row = reprobe.run_sso_probe_cycle(
                email,
                sso,
                password=passwd if args.enable_relogin else "",
                max_proxies=max(1, int(args.max_proxies)),
                extra_proxies=extra_proxies,
                config=cfg,
                profile=profile,
                log_callback=_log,
                dry_run=bool(args.dry_run),
            )
        elif creds and not args.no_refresh:
            row = reprobe.run_account_cycle(
                email,
                creds,
                max_proxies=max(1, int(args.max_proxies)),
                enable_refresh=True,
                extra_proxies=extra_proxies,
                config=cfg,
                profile=profile,
                refresh_fn=None if args.dry_run else refresh_access_token,
                log_callback=_log,
                dry_run=bool(args.dry_run),
            )
        else:
            row = {
                "email": email,
                "final_status": "skipped_no_credentials",
                "error": "no sso token, no build token or refresh disabled",
            }

        # 收集 chenyme grok2api 导入格式（live_sso / live_relogin 才有新 tokens）
        if row.get("final_status") in ("live_sso", "live_relogin"):
            tokens = row.get("build_tokens") or {}
            access = str(tokens.get("access_token") or "").strip()
            refresh = str(tokens.get("refresh_token") or "").strip()
            if access and refresh:
                claims = _decode_jwt_payload(access)
                exp = int(claims.get("exp") or 0)
                sub = str(claims.get("sub") or claims.get("principal_id") or "").strip()
                team_id = str(claims.get("team_id") or "").strip()
                expires_in = int(tokens.get("expires_in") or 0)
                if exp:
                    expires_at = datetime.utcfromtimestamp(exp).strftime("%Y-%m-%dT%H:%M:%SZ")
                elif expires_in:
                    expires_at = datetime.utcfromtimestamp(
                        int(time.time()) + expires_in
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    expires_at = ""
                import_accounts.append({
                    "provider": "grok_build",
                    "name": email,
                    "client_id": "b1a00492-073a-47ea-816f-4c329264a828",
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": str(tokens.get("id_token") or ""),
                    "token_type": "Bearer",
                    "scope": "",
                    "expires_at": expires_at,
                    "expires_in": expires_in,
                    "email": email,
                    "user_id": sub,
                    "principal_id": sub,
                    "team_id": team_id,
                })

        # 写 jsonl 时不带内部 tokens
        rows.append(row)
        clean_row = {k: v for k, v in row.items() if k != "build_tokens"}
        try:
            bl.append_liveness_jsonl(out_path, clean_row)
        except Exception as exc:
            _log(f"[Debug] 写结果失败: {exc}")

    summary = reprobe.summarize_results(rows)
    parts = [f"{k}={v}" for k, v in sorted(summary.items()) if k != "total"]
    _log(f"[+] 完成 total={summary.get('total', 0)} " + " ".join(parts))
    _log(f"[+] 结果: {out_path}")
    if import_accounts:
        try:
            with open(import_path, "w", encoding="utf-8") as handle:
                json.dump({"accounts": import_accounts}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            _log(f"[+] 可导入 grok2api Build ({len(import_accounts)} 个): {import_path}")
        except Exception as exc:
            _log(f"[!] 写导入 JSON 失败: {exc}")
    else:
        _log("[*] 无可用 Build tokens 生成导入文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
