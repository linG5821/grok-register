#!/usr/bin/env python3
"""从 ClashMeta 订阅生成 mihomo 完整配置 + grok-register proxy_pool。

为何必须 inline proxies
-----------------------
listeners 的 proxy: 字段只能引用「配置里已存在的代理/组名」。
proxy-providers 异步加载的节点在 listener 解析时常常还不可见，
日志会出现: parse failed: proxy XXX not found。
因此本脚本把订阅节点直接写进 proxies:，listeners 再引用同名。

用法:
  python3 scripts/gen_mihomo_listeners.py --url http://HOST:PORT/download/sub?target=ClashMeta
  python3 scripts/gen_mihomo_listeners.py --host 192.168.1.10 --listen 0.0.0.0
  python3 scripts/gen_mihomo_listeners.py --types http,vless --limit 30
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_LISTENERS = ROOT / "scripts" / "mihomo-listeners.generated.yaml"
OUT_POOL = ROOT / "scripts" / "proxy_pool.generated.json"
OUT_MIHOMO = ROOT / "scripts" / "mihomo-pool.yaml"
OUT_SNIPPET = ROOT / "scripts" / "config.proxy_pool.snippet.json"

# 无默认内网地址/密钥；必须由调用方传入
DEFAULT_URL = ""
DEFAULT_HOST = "127.0.0.1"
DEFAULT_LISTEN = "0.0.0.0"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "grok-register/gen-listeners"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_proxies(text: str) -> list[dict]:
    proxies: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("- {"):
            continue
        raw = line[2:].strip()
        try:
            proxies.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    if proxies:
        return proxies
    raise SystemExit("无法解析订阅：未找到 `- {json}` 形式的 proxies 行")


def yaml_quote(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def proxy_to_yaml(proxy: dict, indent: str = "  ") -> str:
    """输出标准 YAML map（不用 JSON 单行，避免特殊字符/兼容问题）。"""
    name = str(proxy.get("name") or "unnamed")
    lines = [f"{indent}- name: {yaml_quote(name)}"]
    # 固定 type 先写
    typ = proxy.get("type")
    if typ is not None:
        lines.append(f"{indent}  type: {typ}")
    for key, value in proxy.items():
        if key in ("name", "type"):
            continue
        lines.append(_yaml_kv(key, value, indent + "  "))
    return "\n".join(lines)


def _yaml_kv(key: str, value, indent: str) -> str:
    if isinstance(value, bool):
        return f"{indent}{key}: {'true' if value else 'false'}"
    if value is None:
        return f"{indent}{key}: null"
    if isinstance(value, (int, float)):
        return f"{indent}{key}: {value}"
    if isinstance(value, str):
        # 简单字符串尽量不引号；含特殊字符则单引号
        if value == "" or any(c in value for c in ":#{}[],&*!|>%@`'\"\n") or value.strip() != value:
            return f"{indent}{key}: {yaml_quote(value)}"
        return f"{indent}{key}: {value}"
    if isinstance(value, dict):
        if not value:
            return f"{indent}{key}: {{}}"
        parts = [f"{indent}{key}:"]
        for k, v in value.items():
            parts.append(_yaml_kv(str(k), v, indent + "  "))
        return "\n".join(parts)
    if isinstance(value, list):
        if not value:
            return f"{indent}{key}: []"
        parts = [f"{indent}{key}:"]
        for item in value:
            if isinstance(item, (dict, list)):
                parts.append(f"{indent}  -")
                # rare; dump json fallback
                parts.append(f"{indent}    {json.dumps(item, ensure_ascii=False)}")
            else:
                parts.append(_yaml_kv("", item, indent + "  ").replace(f"{indent}  : ", f"{indent}  - "))
        return "\n".join(parts)
    return f"{indent}{key}: {yaml_quote(str(value))}"


def sanitize_proxy_name(index: int, original: str) -> str:
    """给 listener 用稳定短名，避免 emoji/测速后缀导致匹配失败。"""
    return f"node-{index + 1:03d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="ClashMeta 订阅 URL（必填，例如 http://host:port/download/sub?target=ClashMeta）",
    )
    ap.add_argument("--base-port", type=int, default=10801)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--types", default="")
    ap.add_argument("--listen", default=DEFAULT_LISTEN)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument(
        "--secret",
        default="",
        help="mihomo external-controller secret；空则每次随机生成 32 hex",
    )
    ap.add_argument(
        "--listener-type",
        default="mixed",
        choices=("mixed", "socks", "http"),
        help="mihomo listener 类型；mixed/http 可给 Chromium 用 http:// 代理（推荐 mixed）",
    )
    ap.add_argument(
        "--pool-scheme",
        default="http",
        help="写入 proxy_pool 的 URL scheme（默认 http，配合 mixed/http listener；socks 用 socks5）",
    )
    ap.add_argument(
        "--keep-names",
        action="store_true",
        help="保留订阅原始 name（默认改成 node-001 稳定名，避免特殊字符）",
    )
    args = ap.parse_args()
    if not str(args.url or "").strip():
        raise SystemExit("请用 --url 指定 ClashMeta 订阅地址")
    if not str(args.secret or "").strip():
        args.secret = secrets.token_hex(16)

    print(f"[*] fetch {args.url}", file=sys.stderr)
    text = fetch(args.url)
    proxies = parse_proxies(text)
    print(f"[*] parsed {len(proxies)} proxies", file=sys.stderr)

    allow = {t.strip().lower() for t in args.types.split(",") if t.strip()}
    if allow:
        proxies = [p for p in proxies if str(p.get("type", "")).lower() in allow]
        print(f"[*] after type filter {allow}: {len(proxies)}", file=sys.stderr)

    if args.limit > 0:
        proxies = proxies[: args.limit]
        print(f"[*] after limit: {len(proxies)}", file=sys.stderr)

    if not proxies:
        raise SystemExit("过滤后无节点")

    # 重命名为稳定 name，listeners 与 proxies 一致
    renamed: list[dict] = []
    for i, p in enumerate(proxies):
        item = dict(p)
        original = str(item.get("name") or f"node-{i}")
        if args.keep_names:
            stable = original
        else:
            stable = sanitize_proxy_name(i, original)
            item["_original_name"] = original  # 仅注释用，写出前删
        item["name"] = stable
        renamed.append(item)

    proxy_lines: list[str] = []
    listener_lines: list[str] = []
    pool: list[str] = []
    name_list: list[str] = []

    for i, item in enumerate(renamed):
        original = item.pop("_original_name", item["name"])
        port = args.base_port + i
        name = item["name"]
        name_list.append(name)
        proxy_lines.append(f"  # orig: {original}")
        proxy_lines.append(proxy_to_yaml(item))
        proxy_lines.append("")
        listener_lines.extend(
            [
                f"  - name: pool-{i + 1:03d}",
                f"    type: {args.listener_type}",
                f"    port: {port}",
                f"    listen: {args.listen}",
                f"    proxy: {yaml_quote(name)}",
                "",
            ]
        )
        scheme = (args.pool_scheme or "http").strip().lower() or "http"
        pool.append(f"{scheme}://{args.host}:{port}")

    end = args.base_port + len(renamed) - 1
    names_yaml = "\n".join(f"      - {yaml_quote(n)}" for n in name_list)

    cfg = f"""# ============================================================
# mihomo — grok-register 代理池（inline proxies，listeners 可解析）
# 重生成: python3 scripts/gen_mihomo_listeners.py --host {args.host}
# 端口: mixed 31506 | socks 31507 | api 31508 | pool {args.base_port}-{end}
# proxy_pool 客户端: {args.host}
# ============================================================

allow-lan: true
bind-address: "*"
ipv6: false

mode: rule
log-level: info

socks-port: 31507
mixed-port: 31506

external-controller: 0.0.0.0:31508
secret: "{args.secret}"

# 节点内联（不要只用 proxy-providers，否则 listeners 报 not found）
proxies:
{chr(10).join(proxy_lines)}
proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - ALL
      - DIRECT
  - name: ALL
    type: select
    proxies:
{names_yaml}

listeners:
{chr(10).join(listener_lines)}
rules:
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - MATCH,DIRECT
"""
    OUT_MIHOMO.write_text(cfg, encoding="utf-8")
    OUT_LISTENERS.write_text("listeners:\n" + "\n".join(listener_lines) + "\n", encoding="utf-8")
    OUT_POOL.write_text(json.dumps(pool, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    snippet = {
        "proxy": "",
        "proxy_pool": pool,
        "proxy_pool_strategy": "round_robin",
        "proxy_ipcheck": True,
    }
    OUT_SNIPPET.write_text(json.dumps(snippet, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"[+] {OUT_MIHOMO} ({len(renamed)} proxies + listeners, {args.base_port}-{end})")
    print(f"[+] {OUT_POOL} host={args.host}")
    print(f"[+] {OUT_SNIPPET}")
    print("[!] 节点已重命名为 node-001..；更新订阅后重跑本脚本并重载 mihomo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
