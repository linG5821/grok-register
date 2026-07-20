#!/usr/bin/env python3
"""对邮箱列表中的 Build 号做换代理真活 + 官方 refresh 复测。

用法::

    python scripts/reprobe_build_liveness.py --emails emails.txt
    python scripts/reprobe_build_liveness.py --emails emails.txt --dry-run
    python scripts/reprobe_build_liveness.py --emails emails.txt --max-proxies 5 --no-refresh
"""
from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(description="Build 403 号换代理复测 + 官方 refresh")
    parser.add_argument("--emails", required=True, help="邮箱列表文件，每行一个")
    parser.add_argument("--max-proxies", type=int, default=5, help="每 phase 最多代理数（默认 5）")
    parser.add_argument("--model", default="", help="覆盖 build_liveness_model")
    parser.add_argument("--no-refresh", action="store_true", help="跳过官方 refresh")
    parser.add_argument("--dry-run", action="store_true", help="只匹配凭据/列代理，不探测")
    parser.add_argument("--proxy-file", default="", help="额外代理 URL 列表")
    parser.add_argument("--out-dir", default="", help="结果目录，默认项目根")
    parser.add_argument("--config", default="", help="config.json 路径")
    args = parser.parse_args(argv)

    config_path = str(args.config or "").strip()
    if config_path:
        os.environ.setdefault("GROK_REGISTER_CONFIG", config_path)
        # app_config 使用模块级 CONFIG_FILE；临时 monkey 不够稳，直接 load 后用
    cfg = load_config()
    if args.model:
        cfg["build_liveness_model"] = str(args.model).strip()

    base = str(cfg.get("chenyme_grok2api_base") or "").strip().rstrip("/")
    username = str(cfg.get("chenyme_grok2api_username") or "").strip()
    password = str(cfg.get("chenyme_grok2api_password") or "").strip()
    if not base or not username or not password:
        print("请在 config.json 配置 chenyme_grok2api_base/username/password", file=sys.stderr)
        return 2

    emails_path = os.path.abspath(args.emails)
    if not os.path.isfile(emails_path):
        print(f"邮箱文件不存在: {emails_path}", file=sys.stderr)
        return 2

    emails = reprobe.load_emails_file(emails_path)
    if not emails:
        print("邮箱列表为空", file=sys.stderr)
        return 2
    _log(f"[*] 目标邮箱 {len(emails)} 个")

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
    index = reprobe.index_build_accounts(exported)
    _log(f"[*] export Build 号 {len(index)} 个；开始匹配/复测")

    profile = fetch_cli_profile(cfg, base, admin_token)
    _log(
        f"[*] CLI profile version={profile.get('client_version')} "
        f"ua={str(profile.get('user_agent') or '')[:40]}"
    )

    out_dir = str(args.out_dir or "").strip() or ROOT
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"reprobe_{stamp}.jsonl")

    rows: list[dict[str, Any]] = []
    for i, email in enumerate(emails, 1):
        _log(f"--- [{i}/{len(emails)}] {email} ---")
        creds = index.get(email)
        row = reprobe.run_account_cycle(
            email,
            creds,
            max_proxies=max(1, int(args.max_proxies)),
            enable_refresh=not args.no_refresh,
            extra_proxies=extra_proxies,
            config=cfg,
            profile=profile,
            refresh_fn=None if args.dry_run else refresh_access_token,
            log_callback=_log,
            dry_run=bool(args.dry_run),
        )
        rows.append(row)
        try:
            bl.append_liveness_jsonl(out_path, row)
        except Exception as exc:
            _log(f"[Debug] 写结果失败: {exc}")

    summary = reprobe.summarize_results(rows)
    parts = [f"{k}={v}" for k, v in sorted(summary.items()) if k != "total"]
    _log(f"[+] 完成 total={summary.get('total', 0)} " + " ".join(parts))
    _log(f"[+] 结果: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
