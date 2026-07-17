"""从 accounts_*.txt 批量补开 NSFW。

行格式与 append_account_line 一致：email----password----sso
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple


EnableNsfwFn = Callable[..., Tuple[bool, str]]
LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]


@dataclass
class AccountRecord:
    email: str
    password: str
    sso: str
    line_no: int = 0


@dataclass
class BackfillResult:
    total_lines: int = 0
    parsed: int = 0
    skipped: int = 0
    success: int = 0
    failed: int = 0
    cancelled: bool = False
    failures: List[Tuple[str, str]] = field(default_factory=list)
    successes: List[str] = field(default_factory=list)


def normalize_sso(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("sso="):
        token = token[4:].strip()
    return token


def parse_accounts_line(line: str, line_no: int = 0) -> Optional[AccountRecord]:
    text = str(line or "").strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split("----", 2)
    if len(parts) < 3:
        return None
    email = parts[0].strip()
    password = parts[1].strip()
    sso = normalize_sso(parts[2])
    if not email or not sso:
        return None
    return AccountRecord(email=email, password=password, sso=sso, line_no=line_no)


def load_accounts_file(path: str) -> Tuple[List[AccountRecord], int, int]:
    """返回 (去重后的账号列表, 总行数, 跳过行数)。同 sso 只保留首次出现。"""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"账号文件不存在: {path}")
    records: List[AccountRecord] = []
    seen = set()
    total = 0
    skipped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for idx, raw in enumerate(handle, start=1):
            total += 1
            rec = parse_accounts_line(raw, line_no=idx)
            if rec is None:
                skipped += 1
                continue
            if rec.sso in seen:
                skipped += 1
                continue
            seen.add(rec.sso)
            records.append(rec)
    return records, total, skipped


def backfill_nsfw_from_accounts(
    path: str,
    enable_nsfw: EnableNsfwFn,
    log_callback: Optional[LogFn] = None,
    cancel_callback: Optional[CancelFn] = None,
    delay_sec: float = 1.0,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> BackfillResult:
    """读取 accounts 文件并对每个 SSO 调用 enable_nsfw(sso, log_callback=...).

    enable_nsfw 签名兼容 registration_browser.enable_nsfw_for_token。
    """
    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    def cancelled() -> bool:
        if not cancel_callback:
            return False
        try:
            return bool(cancel_callback())
        except Exception:
            return False

    sleeper = sleep_fn or (lambda _s: None)
    result = BackfillResult()
    records, total, skipped = load_accounts_file(path)
    result.total_lines = total
    result.skipped = skipped
    result.parsed = len(records)
    log(f"[*] NSFW 补开：文件={path} 有效={len(records)} 跳过={skipped} 总行={total}")

    for i, rec in enumerate(records, start=1):
        if cancelled():
            result.cancelled = True
            log("[!] NSFW 补开已停止")
            break
        log(f"[*] ({i}/{len(records)}) 补开 NSFW: {rec.email}")
        try:
            ok, message = enable_nsfw(rec.sso, log_callback=log_callback)
        except TypeError:
            # 兼容只接收 token 的简单 mock
            try:
                ok, message = enable_nsfw(rec.sso)
            except Exception as exc:
                ok, message = False, str(exc)
        except Exception as exc:
            ok, message = False, str(exc)
        if ok:
            result.success += 1
            result.successes.append(rec.email)
            log(f"[+] NSFW 补开成功: {rec.email} ({message})")
        else:
            result.failed += 1
            result.failures.append((rec.email, str(message or "unknown")))
            log(f"[-] NSFW 补开失败: {rec.email}: {message}")
        if i < len(records) and delay_sec > 0 and not cancelled():
            try:
                sleeper(float(delay_sec))
            except Exception:
                pass

    log(
        f"[*] NSFW 补开结束：成功={result.success} 失败={result.failed} "
        f"跳过={result.skipped} 取消={result.cancelled}"
    )
    return result


def dry_run_validate_file(path: str) -> BackfillResult:
    """只解析不调用网络，用于验证文件格式是否可走通。"""
    records, total, skipped = load_accounts_file(path)
    result = BackfillResult(
        total_lines=total,
        parsed=len(records),
        skipped=skipped,
        success=0,
        failed=0,
    )
    return result
