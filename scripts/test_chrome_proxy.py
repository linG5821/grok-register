#!/usr/bin/env python3
"""Windows 上单独测：本机代理桥 + Chromium CDP 是否能起来。

用法（在 Windows 项目目录）:
  python scripts/test_chrome_proxy.py --proxy http://127.0.0.1:10801
  python scripts/test_chrome_proxy.py --proxy http://YOUR_MIHOMO_HOST:10801
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--proxy",
        default="http://127.0.0.1:10801",
        help="上游代理 URL（本机桥会再包一层）",
    )
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    sys.path.insert(0, str(__file__).rsplit("scripts", 1)[0].rstrip("\\/"))

    from cpa_xai.proxyutil import prepare_chromium_proxy
    from browser_runtime import create_browser_options
    from DrissionPage import Chromium

    def log(msg: str) -> None:
        print(msg, flush=True)

    # 模拟注册程序：先 rotate 写 HTTP_PROXY，再在启动瞬间清掉
    try:
        import app_config
        app_config.load_config()
        import proxy_manager as pm
        pm.rotate_session("test-script")
        log(f"[*] after rotate HTTP_PROXY={os.environ.get('HTTP_PROXY')!r}")
    except Exception as exc:
        log(f"[*] skip rotate sim: {exc}")

    local, bridge = prepare_chromium_proxy(args.proxy, log=log)
    log(f"[*] local bridge = {local!r}")
    opts = create_browser_options(browser_proxy=local)
    log(f"[*] address before launch = {opts.address!r}")
    log(f"[*] proxy-related args = {[a for a in opts.arguments if 'proxy' in a.lower()]}")

    t0 = time.time()
    try:
        saved = {}
        try:
            from proxy_manager import clear_proxy_environment, restore_proxy_environment
            saved = clear_proxy_environment()
            log(f"[*] cleared env keys for launch: {list(saved.keys())}")
        except Exception:
            pass
        try:
            browser = Chromium(opts)
        finally:
            try:
                from proxy_manager import restore_proxy_environment
                restore_proxy_environment(saved)
            except Exception:
                pass
        log(f"[+] Chromium OK in {time.time() - t0:.1f}s: {browser}")
        tabs = browser.get_tabs()
        page = tabs[-1] if tabs else browser.new_tab()
        log("[*] tab ok, try open http://ipinfo.io/ip ...")
        try:
            page.get("http://ipinfo.io/ip", timeout=20)
            log(f"[+] page html snippet: {(page.html or '')[:200]!r}")
        except Exception as exc:
            log(f"[!] page.get failed: {exc}")
        browser.quit(del_data=True)
        log("[+] quit ok")
        return 0
    except Exception as exc:
        log(f"[!] FAIL after {time.time() - t0:.1f}s: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if bridge is not None:
            try:
                bridge.stop()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
