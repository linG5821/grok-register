"""本地 CPA 导出后，累加产出 grok2api Build 导入文件。

每号 mint 成功后把凭据追加进单个 ``{"accounts": [...]}`` 文件，
provider=grok_build，格式与 chenyme export / reprobe 导入一致。

- 单文件累加，同 email 覆盖旧条目（去重）
- 原子写（mkstemp → fsync → os.replace），中断不损坏目标
- 目标文件损坏时先备份成 ``<file>.bak`` 再重建，绝不静默丢已有数据
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional


CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

# 注册循环逐号串行；兜住万一的并发调用
_write_lock = threading.Lock()


def _decode_jwt_payload(token: str) -> dict:
    """base64url 解 JWT payload，失败返回 {}。"""
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


def build_import_entry(cpa_payload: dict) -> dict:
    """CPA xai-*.json payload → grok2api Build 导入条目。"""
    payload = dict(cpa_payload or {})
    access = str(payload.get("access_token") or "").strip()
    refresh = str(payload.get("refresh_token") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    sub = str(payload.get("sub") or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    expires_in = int(payload.get("expires_in") or 0)

    # expires_at：优先 CPA 的 expired（RFC3339），缺则用 expires_in 现算
    expires_at = str(payload.get("expired") or "").strip()
    if not expires_at and expires_in > 0:
        expires_at = datetime.fromtimestamp(
            time.time() + expires_in, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # team_id 从 access_token JWT 解，缺则空
    claims = _decode_jwt_payload(access)
    team_id = str(claims.get("team_id") or "").strip()
    if not sub:
        sub = str(claims.get("sub") or claims.get("principal_id") or "").strip()

    return {
        "provider": "grok_build",
        "name": email,
        "email": email,
        "client_id": CLIENT_ID,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "token_type": "Bearer",
        "scope": "",
        "expires_at": expires_at,
        "expires_in": expires_in,
        "user_id": sub,
        "principal_id": sub,
        "team_id": team_id,
    }


def _load_accounts(file_path: str) -> tuple[list[dict], bool]:
    """读现有导入文件。返回 (accounts, corrupted)。

    不存在 → ([], False)；解析失败/结构非法 → ([], True)。
    """
    if not os.path.exists(file_path):
        return [], False
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return [], True
    if not isinstance(data, dict):
        return [], True
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        return [], True
    return [a for a in accounts if isinstance(a, dict)], False


def _atomic_write(file_path: str, accounts: list[dict]) -> None:
    directory = os.path.dirname(os.path.abspath(file_path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".build-import-", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"accounts": accounts}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except Exception:
            pass
        os.replace(temp_path, file_path)
        temp_path = None
        try:
            os.chmod(file_path, 0o600)
        except Exception:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def append_build_import(
    file_path: str,
    entry: dict,
    log: Optional[Any] = None,
) -> None:
    """把一条 Build 导入条目累加进文件；同 email 覆盖。原子写；损坏先备份。"""
    path = str(file_path or "").strip()
    if not path:
        return
    email = str((entry or {}).get("email") or (entry or {}).get("name") or "").strip().lower()
    logger = log if callable(log) else (lambda _m: None)

    with _write_lock:
        accounts, corrupted = _load_accounts(path)
        if corrupted:
            backup = path + ".bak"
            try:
                shutil.copy2(path, backup)
                logger(f"[cpa] 导入文件损坏，已备份 {backup} 后重建")
            except Exception as exc:
                logger(f"[cpa] 导入文件损坏且备份失败（将重建）: {exc}")
            accounts = []

        # 去重：同 email 覆盖旧条目
        replaced = False
        if email:
            for i, acc in enumerate(accounts):
                existing = str(acc.get("email") or acc.get("name") or "").strip().lower()
                if existing == email:
                    accounts[i] = entry
                    replaced = True
                    break
        if not replaced:
            accounts.append(entry)

        _atomic_write(path, accounts)
