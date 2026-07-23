#!/usr/bin/env python3
"""Google Colab 一键注册入口（独立脚本，不改仓库其它代码）。

- 强制无代理，直接用 Colab 宿主出口 IP
- 启动前给 DrissionPage 补 Colab 必需的 headless / no-sandbox 参数（仅本进程 monkeypatch）
- 可选：探测到机房 ASN 或 bot 率过高时尝试 unassign Runtime（需手动重新连接换机）

用法（在项目根目录）::

    python colab/run_colab_register.py --count 3
    python colab/run_colab_register.py --count 5 --rotate-after 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path


# 可能被 ensure_project_root 纠正（嵌套 clone / 工作目录不同时）
ROOT = Path(__file__).resolve().parents[1]


def _log(msg: str) -> None:
    print(msg, flush=True)


def ensure_project_root() -> Path:
    """定位含 app_config.py 的项目根，chdir + 插入 sys.path。失败直接退出。"""
    global ROOT
    candidates = [
        Path(__file__).resolve().parents[1],
        Path.cwd(),
        Path.cwd() / "grok-register",
        Path("/content/grok-register"),
        Path("/content") / "grok-register",
        Path(__file__).resolve().parents[2] / "grok-register",
    ]
    seen: set[str] = set()
    found: Path | None = None
    for cand in candidates:
        try:
            root = cand.resolve()
        except Exception:
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if (root / "app_config.py").is_file() and (root / "browser_runtime.py").is_file():
            found = root
            break
    if found is None:
        _log("[colab] 找不到项目根（需要 app_config.py + browser_runtime.py）")
        _log(f"[colab] __file__={__file__}")
        _log(f"[colab] cwd={os.getcwd()}")
        _log(f"[colab] 已试: {list(seen)[:8]}")
        _log("[colab] 请确认已 clone 完整仓库到 /content/grok-register")
        raise SystemExit(2)
    ROOT = found
    os.chdir(ROOT)
    root_s = str(ROOT)
    # 始终插到最前，避免被其它路径遮蔽
    while root_s in sys.path:
        sys.path.remove(root_s)
    sys.path.insert(0, root_s)
    os.environ["PYTHONPATH"] = root_s + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )
    _log(f"[colab] project root = {ROOT}")
    _log(f"[colab] sys.path[0] = {sys.path[0]}")
    return ROOT


def find_browser_binary() -> str:
    """Colab/Linux 常见 Chromium/Chrome 路径。"""
    import shutil

    candidates = [
        os.environ.get("CHROME_BIN") or "",
        os.environ.get("BROWSER_PATH") or "",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
        shutil.which("google-chrome-stable") or "",
        shutil.which("google-chrome") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("chromium") or "",
    ]
    for path in candidates:
        text = str(path or "").strip()
        if text and os.path.isfile(text) and os.access(text, os.X_OK):
            return text
    return ""


def ensure_xvfb(display: str = ":99") -> bool:
    """启动虚拟显示，使「有头」Chrome 在无 GUI 的 Colab 上可跑。

    真 headless 常被 Cloudflare 直接拦；别人 Colab 能跑多半是 Xvfb + 有头 Chrome。
    """
    import shutil
    import subprocess

    if os.environ.get("DISPLAY"):
        _log(f"[colab] DISPLAY 已存在: {os.environ['DISPLAY']}")
        return True
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        _log("[colab] 未安装 Xvfb，请: apt-get install -y xvfb")
        return False
    # 已有进程则复用
    try:
        out = subprocess.check_output(["pgrep", "-a", "Xvfb"], text=True, stderr=subprocess.DEVNULL)
        if display in out or "Xvfb" in out:
            os.environ["DISPLAY"] = display
            _log(f"[colab] 复用已有 Xvfb, DISPLAY={display}")
            return True
    except Exception:
        pass
    log_path = "/tmp/xvfb-colab.log"
    try:
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                [xvfb, display, "-screen", "0", "1280x900x24", "-ac"],
                stdout=logf,
                stderr=logf,
                start_new_session=True,
            )
        time.sleep(0.8)
        os.environ["DISPLAY"] = display
        _log(f"[colab] 已启动 Xvfb DISPLAY={display}")
        return True
    except Exception as exc:
        _log(f"[colab] 启动 Xvfb 失败: {exc}")
        return False


def patch_cf_fail_fast() -> None:
    """检测到 Cloudflare Attention Required 时立刻失败，避免空转找按钮。"""
    try:
        import registration_browser as rb
    except Exception as exc:
        _log(f"[colab] 无法 patch CF 检测: {exc}")
        return

    original = getattr(rb, "click_email_signup_button", None)
    if not callable(original):
        return

    def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
        page = getattr(rb, "page", None)
        if page is not None:
            try:
                html = ""
                try:
                    html = str(page.html or "")[:4000]
                except Exception:
                    pass
                title = ""
                try:
                    title = str(page.title or "")
                except Exception:
                    try:
                        title = str(page.run_js("return document.title") or "")
                    except Exception:
                        title = ""
                blob = (title + "\n" + html).lower()
                if (
                    "attention required" in blob
                    or "cf-error-details" in blob
                    or "sorry, you have been blocked" in blob
                    or ("cloudflare" in blob and "just a moment" in blob)
                ):
                    msg = (
                        "Cloudflare 拦截页（Attention Required / blocked），"
                        "当前无注册按钮可点。请换 Runtime 或改用住宅代理。"
                    )
                    if log_callback:
                        log_callback(f"[!] {msg} title={title!r}")
                    raise RuntimeError(msg)
            except RuntimeError:
                raise
            except Exception:
                pass
        return original(timeout=timeout, log_callback=log_callback, cancel_callback=cancel_callback)

    rb.click_email_signup_button = click_email_signup_button
    _log("[colab] 已启用 Cloudflare 拦截早失败")


def patch_browser_for_colab(*, use_headless: bool = False) -> None:
    """仅在本进程内给 create_browser_options 打补丁，不修改源文件。

    默认 **有头 + Xvfb**（use_headless=False）：真 headless 极易被 CF 拦，
    且 Chrome 在 headless 下通常不加载扩展。
    """
    import browser_runtime as br

    browser_bin = find_browser_binary()
    if not browser_bin:
        _log("[colab] 未找到 Chrome/Chromium 可执行文件")
        _log("[colab] 请先运行安装格，或手动：")
        _log("  !wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb")
        _log("  !apt-get install -y -qq ./google-chrome-stable_current_amd64.deb")
        raise RuntimeError("browser executable not found")

    if not use_headless:
        if not ensure_xvfb():
            _log("[colab] 无 Xvfb 时回退 headless（更易被 CF 拦）")
            use_headless = True
    else:
        _log("[colab] 使用真 headless（不推荐，易被 Cloudflare 拦）")

    # 扩展：有头模式才能加载；路径按项目根解析
    ext = ROOT / "turnstilePatch"
    if not ext.is_dir():
        _log(f"[colab] 警告: 未找到 turnstilePatch 扩展目录: {ext}")
        _log("[colab] 本地若有该目录请放进仓库根，或接受无扩展过盾更难")

    original = br.create_browser_options

    def create_browser_options(browser_proxy="", extension_path=None):
        # 优先用仓库 turnstilePatch
        if extension_path is None and ext.is_dir():
            extension_path = str(ext)
        options = original(browser_proxy=browser_proxy, extension_path=extension_path)
        try:
            options.set_browser_path(browser_bin)
        except Exception as exc:
            _log(f"[colab] set_browser_path 失败: {exc}")
            raise
        for arg in (
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1280,900",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=en-US",
        ):
            try:
                options.set_argument(arg)
            except Exception:
                pass
        # 有头：明确关掉 headless；headless 才开
        if use_headless:
            try:
                options.headless(True)
            except Exception:
                try:
                    options.set_argument("--headless=new")
                except Exception:
                    pass
            try:
                options.set_argument("--disable-gpu")
            except Exception:
                pass
        else:
            try:
                options.headless(False)
            except Exception:
                pass
            # 去掉可能被默认带上的 headless 参数
            try:
                args = list(getattr(options, "arguments", []) or [])
                cleaned = [a for a in args if "headless" not in str(a).lower()]
                if cleaned != args and hasattr(options, "set_argument"):
                    # ChromiumOptions 可能无法直接删参数；尽力 set headless False 即可
                    pass
            except Exception:
                pass
        try:
            if hasattr(options, "set_user_agent"):
                options.set_user_agent(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
        except Exception:
            pass
        mode = "headless" if use_headless else f"headed+Xvfb(DISPLAY={os.environ.get('DISPLAY')})"
        _log(f"[colab] browser: path={browser_bin} mode={mode}")
        return options

    br.create_browser_options = create_browser_options
    _log(f"[colab] 已绑定浏览器: {browser_bin}")


def force_no_proxy_config(cfg: dict) -> dict:
    """写回内存配置：清空代理，关健康扫描。"""
    cfg = dict(cfg)
    cfg["proxy"] = ""
    cfg["proxy_pool"] = []
    cfg["proxy_health_enabled"] = False
    cfg["proxy_ipcheck"] = True  # 仍可做出口自检日志
    cfg["remote_import_use_proxy"] = False
    cfg["mail_use_proxy"] = False
    cfg["cpa_proxy"] = ""
    # Colab 上默认不走远程 convert（机房 IP 叠服务器更易 bot）；CPA 可按配置开
    if "chenyme_grok2api_convert" in cfg:
        # 保留用户 config 值；入口参数可再覆盖
        pass
    return cfg


def probe_egress() -> dict:
    """探测当前出口（直连）。"""
    try:
        from curl_cffi import requests as crequests

        resp = crequests.get("https://ipinfo.io/json", timeout=15, proxies={})
        data = resp.json() if hasattr(resp, "json") else {}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    org = str(data.get("org") or "")
    hosting = any(
        m in org.lower()
        for m in (
            "cloud", "hosting", "server", "data center", "datacenter",
            "google", "amazon", "aws", "microsoft", "azure", "digitalocean",
            "ovh", "hetzner", "linode", "vultr", "colocation",
        )
    )
    return {
        "ok": True,
        "ip": str(data.get("ip") or ""),
        "country": str(data.get("country") or ""),
        "city": str(data.get("city") or ""),
        "org": org,
        "hosting": hosting,
    }


def try_rotate_runtime(reason: str) -> bool:
    """尝试断开 Colab Runtime，下次重连通常会换机器/IP。

    注意：Colab **没有**官方「无感自动换机并继续跑」API。
    unassign 会断开会话，需要你手动重新连接并再跑一次本脚本。
    """
    _log(f"[colab] 请求切换宿主: {reason}")
    try:
        from google.colab import runtime  # type: ignore

        _log("[colab] 正在 unassign Runtime（会话将断开，请重新连接后再次运行）…")
        time.sleep(1.0)
        runtime.unassign()
        return True
    except ImportError:
        _log("[colab] 非 Colab 环境，无法自动换机。请手动换网络/机器。")
        return False
    except Exception as exc:
        _log(f"[colab] unassign 失败: {exc}")
        _log("[colab] 请手动: 菜单 Runtime → Disconnect and delete runtime → 再连接")
        return False


def apply_runtime_config(
    count: int,
    *,
    disable_chenyme_convert: bool,
    enable_cpa: bool | None,
) -> None:
    from app_config import config, load_config, save_config, validate_run_requirements, ConfigError

    load_config()
    cfg = force_no_proxy_config(dict(config))
    cfg["register_count"] = max(1, int(count))
    if disable_chenyme_convert:
        cfg["chenyme_grok2api_convert"] = False
    if enable_cpa is not None:
        cfg["cpa_export_enabled"] = bool(enable_cpa)

    config.clear()
    config.update(cfg)
    try:
        validated = validate_run_requirements(config)
        config.clear()
        config.update(validated)
    except ConfigError as exc:
        # Colab 上邮箱等可能未配全；仍允许跑，由注册循环自己报错
        _log(f"[colab] 配置校验警告（继续）: {exc}")

    # 落盘一份便于下载对照（不覆盖用户密钥：合并写）
    try:
        save_config()
        _log("[colab] 已写入 config.json（proxy 已清空）")
    except Exception as exc:
        _log(f"[colab] 保存 config 失败（仅用内存配置）: {exc}")


def run_batch(count: int) -> int:
    from grok_register_ttk import run_registration_cli

    _log(f"[colab] 开始注册 count={count}（无代理 / Colab 出口）")
    try:
        run_registration_cli(count)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Colab 无代理注册入口")
    parser.add_argument("--count", type=int, default=1, help="本机注册数量")
    parser.add_argument(
        "--rotate-after",
        type=int,
        default=0,
        help="每注册 N 个号后尝试 unassign Runtime 换机（0=不自动换机）",
    )
    parser.add_argument(
        "--rotate-if-hosting",
        action="store_true",
        help="启动时若探测到机房 ASN，先尝试换机再退出（需手动重连再跑）",
    )
    parser.add_argument(
        "--keep-chenyme-convert",
        action="store_true",
        help="保留 chenyme convert（默认关闭，避免远程机房 convert 打 bot）",
    )
    parser.add_argument(
        "--enable-cpa",
        action="store_true",
        help="强制开启本地 CPA 导出（默认跟随 config.json）",
    )
    parser.add_argument(
        "--disable-cpa",
        action="store_true",
        help="强制关闭 CPA 导出",
    )
    parser.add_argument(
        "--skip-browser-patch",
        action="store_true",
        help="不打浏览器补丁（调试用）",
    )
    parser.add_argument(
        "--true-headless",
        action="store_true",
        help="真 headless（默认用 Xvfb 有头；真 headless 更易被 CF 拦）",
    )
    args = parser.parse_args(argv)

    ensure_project_root()

    if not args.skip_browser_patch:
        try:
            patch_browser_for_colab(use_headless=bool(args.true_headless))
            patch_cf_fail_fast()
        except Exception as exc:
            _log(f"[colab] browser patch 失败: {exc}")
            _log("[colab] 请确认仓库完整、已装 Chrome/Xvfb/DrissionPage")
            return 2

    egress = probe_egress()
    if egress.get("ok"):
        _log(
            f"[colab] 出口 IP={egress.get('ip')} {egress.get('country')}/{egress.get('city')} "
            f"org={egress.get('org')} hosting={egress.get('hosting')}"
        )
        if egress.get("hosting"):
            _log("[colab] 警告: 当前出口像机房/托管 ASN，xAI 更容易打 bot_flag")
            if args.rotate_if_hosting:
                try_rotate_runtime("egress is hosting ASN")
                return 2
    else:
        _log(f"[colab] 出口探测失败: {egress.get('error')}")

    enable_cpa = None
    if args.enable_cpa:
        enable_cpa = True
    elif args.disable_cpa:
        enable_cpa = False

    apply_runtime_config(
        args.count,
        disable_chenyme_convert=not args.keep_chenyme_convert,
        enable_cpa=enable_cpa,
    )

    # 分批：每 rotate_after 个跑完尝试换机
    total = max(1, int(args.count))
    rotate_every = max(0, int(args.rotate_after or 0))
    if rotate_every <= 0:
        return run_batch(total)

    done = 0
    while done < total:
        chunk = min(rotate_every, total - done)
        code = run_batch(chunk)
        done += chunk
        if done >= total:
            return code
        _log(f"[colab] 已完成 {done}/{total}，准备换机…")
        try_rotate_runtime(f"rotate-after {rotate_every}")
        # unassign 后进程通常已死；若没死则退出让用户重连
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
