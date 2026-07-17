"""删除 chenyme/grok2api 中被 xAI 反滥用系统标记 bot 的 Build 账号。

xAI 在签发 access_token 时，会在 JWT payload 中写入 ``bot_flag_source`` 字段
（数值型，通常为 1），一旦出现，所有 ``https://cli-chat-proxy.grok.com/v1/*``
端点会直接返回 403 Access denied——``/billing``、``/chat/completions``、
``/responses`` 全部不可用。此类账号无法通过 refresh token 修复，只能弃用。

用法::

    python scripts/purge_bot_accounts.py --dry-run
    python scripts/purge_bot_accounts.py

脚本只删 ``provider == grok_build``、并且 ``access_token`` 的 JWT payload 里
存在 ``bot_flag_source`` 字段的账号；同时依据 ``linkedAccountId`` 一并处理
对应的 Web 影子账号（因为 chenyme 转换后 Web 号只是 Build 的凭据来源，
Build 号被删后 Web 号也没意义了，避免污染号池）。

配置来源与本项目其他脚本一致：从 ``config.json`` 读 ``chenyme_grok2api_base``、
``chenyme_grok2api_username``、``chenyme_grok2api_password``。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from typing import Any

from curl_cffi import requests


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "config.json")


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        print(f"配置不存在: {CONFIG_FILE}", file=sys.stderr)
        sys.exit(2)
    with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def decode_jwt_payload(token: str) -> dict:
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


def login(base: str, username: str, password: str) -> str:
    endpoint = f"{base}/api/admin/v1/auth/login"
    resp = requests.post(endpoint, json={"username": username, "password": password}, timeout=30)
    resp.raise_for_status()
    payload = resp.json() or {}
    token = payload.get("data", {}).get("tokens", {}).get("accessToken", "")
    if not token:
        raise RuntimeError("登录响应缺少 accessToken")
    return token


def list_accounts_all(base: str, token: str) -> list[dict]:
    """通过 /accounts 分页拉全量账号——export 只给凭据字段，缺 id/linkedAccountId。"""
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            f"{base}/api/admin/v1/accounts",
            headers=headers,
            params={"page": page, "pageSize": 100},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json() or {}
        data = payload.get("data") or {}
        chunk = data.get("items") or []
        items.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
        if page > 100:
            break
    return items


def export_credentials(base: str, token: str) -> list[dict]:
    """从 /accounts/export 拿含 access_token 明文的凭据数组，用于 JWT 解码。"""
    resp = requests.get(
        f"{base}/api/admin/v1/accounts/export",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    return payload.get("accounts") or []


def build_bot_email_set(exported: list[dict]) -> set[str]:
    bots: set[str] = set()
    for account in exported:
        if not isinstance(account, dict):
            continue
        if account.get("provider") != "grok_build":
            continue
        claims = decode_jwt_payload(str(account.get("access_token") or ""))
        if "bot_flag_source" not in claims:
            continue
        email = str(account.get("name", "") or "").strip().lower()
        if email:
            bots.add(email)
    return bots


def delete_account(base: str, token: str, account_id: Any) -> tuple[bool, str]:
    resp = requests.delete(
        f"{base}/api/admin/v1/accounts/{account_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code == 200:
        return True, ""
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="删除被 xAI 标记为 bot 的 grok2api Build 账号")
    parser.add_argument("--dry-run", action="store_true", help="只列出要删的账号，不真删")
    parser.add_argument("--keep-web", action="store_true", help="仅删 Build 号，保留 Web 号")
    args = parser.parse_args()

    cfg = load_config()
    base = str(cfg.get("chenyme_grok2api_base") or "").strip().rstrip("/")
    username = str(cfg.get("chenyme_grok2api_username") or "").strip()
    password = str(cfg.get("chenyme_grok2api_password") or "").strip()
    if not base or not username or not password:
        print("请先在 config.json 里配置 chenyme_grok2api_base/username/password", file=sys.stderr)
        return 2

    print(f"[*] 登录 {base}")
    token = login(base, username, password)

    print("[*] 拉取账号导出（含 access_token 明文）")
    exported = export_credentials(base, token)
    bot_emails = build_bot_email_set(exported)
    if not bot_emails:
        print("[+] 未发现 bot_flag_source 标记的账号，无需处理。")
        return 0
    print(f"[!] 发现 {len(bot_emails)} 个被标记 bot 的 Build 账号：")
    for email in sorted(bot_emails):
        print(f"    - {email}")

    print("[*] 拉取账号列表以定位 ID / linked Web 号")
    listing = list_accounts_all(base, token)
    to_delete: list[tuple[str, str, str]] = []  # (kind, id, name)
    linked_web_ids: set[str] = set()
    for account in listing:
        if not isinstance(account, dict):
            continue
        name = str(account.get("name", "") or "").strip().lower()
        if account.get("provider") != "grok_build":
            continue
        if name not in bot_emails:
            continue
        account_id = str(account.get("id") or "")
        if not account_id:
            continue
        to_delete.append(("build", account_id, name))
        linked_id = str(account.get("linkedAccountId") or "").strip()
        if linked_id and linked_id != "0":
            linked_web_ids.add(linked_id)

    if not args.keep_web and linked_web_ids:
        for account in listing:
            if not isinstance(account, dict):
                continue
            account_id = str(account.get("id") or "")
            if account_id and account_id in linked_web_ids and account.get("provider") == "grok_web":
                to_delete.append(("web-linked", account_id, str(account.get("name", "") or "")))

    if not to_delete:
        print("[!] 定位不到匹配的账号 ID，可能 chenyme 内部数据不一致；请检查后台。")
        return 1

    print(f"[*] 将删除 {len(to_delete)} 个账号：")
    for kind, account_id, name in to_delete:
        print(f"    - [{kind}] id={account_id} {name}")

    if args.dry_run:
        print("[*] dry-run，不执行删除。")
        return 0

    print("[*] 开始删除")
    ok = 0
    failed = 0
    for kind, account_id, name in to_delete:
        success, err = delete_account(base, token, account_id)
        if success:
            ok += 1
            print(f"    [OK ] {kind} id={account_id} {name}")
        else:
            failed += 1
            print(f"    [ERR] {kind} id={account_id} {name}: {err}")
        time.sleep(0.15)

    print(f"[+] 完成：成功 {ok}，失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
