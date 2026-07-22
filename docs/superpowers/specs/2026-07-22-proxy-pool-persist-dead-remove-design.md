# 代理探活 dead 从 config.proxy_pool 删除并持久化

**日期:** 2026-07-22  
**状态:** 已批准  
**范围:** `proxy_manager` + `app_config.save_config`；不改选池策略、不改健康缓存算法本身。

## 1. 问题

`proxy_manager` 健康缓存（available/dead）仅内存。进程退出后下次启动对整池重扫，已确认 dead 的节点仍被探测，浪费时间。

用户要求：把探活/使用失败的代理**从 `config.json` 的 `proxy_pool` 直接删除**，下次启动池子里就没有这些节点。

## 2. 目标

| # | 行为 |
|---|------|
| G1 | 探活失败或 `mark_proxy_dead` 时，从内存 `proxy_pool` 视图与磁盘 `config.json` **移除该 URL** |
| G2 | **只删 dead，永不把 available 写回覆盖整个 pool** |
| G3 | 写盘必须原子（现有 `save_config`：tmp + fsync + replace），中断不丢整份 config |
| G4 | 并发 mark_dead 串行化，避免两次写互相覆盖导致误删 |
| G5 | 写失败仅日志；本进程内存仍标记 dead |

## 3. 非目标

- 不做 dead 列表 / 冷却恢复写回
- 不自动清空单独的 `config.proxy`（仅 pool 条目）
- 不改 TCP/HTTP 探活标准

## 4. 实现要点

### 4.1 API

```python
def remove_dead_from_config(urls: list[str] | set[str], reason: str = "") -> int:
    """从 config.proxy_pool 移除 urls 中出现的原文。返回实际删除条数。
    原子 save_config；new_pool==old 则不写盘。
    """
```

匹配规则：对每条 `proxy_pool` 原文 `entry`，若 `entry in urls` 或 `expand(entry) in urls`（无 rand 时 expand==entry），则删。

带 `{rand}` 的条目：仅当 `entry` 本身在 urls 中时删除（探活路径传入的是 pool 原文）。

### 4.2 调用点

| 位置 | 何时 |
|------|------|
| `_mark_dead_locked` 之后（`mark_proxy_dead` / 探活 `_one` 失败分支） | 每次标 dead 后异步或同步调用 remove（同步 + 锁，简单） |

为避免每条 dead 都全量写 294 项 JSON，可：

- **方案**：进程内攒批 `pending_remove: set`，debounce 0.5s 或每 N 条 flush 一次；进程结束/扫描结束强制 flush。

推荐：**debounce 1s 单线程 timer**，扫描结束 `flush_pending_proxy_removals()`。

### 4.3 写盘

```
with _config_write_lock:
  load/reload 当前 app_config.config
  pool = list(config["proxy_pool"])
  new = [e for e in pool if not should_remove(e)]
  if new == pool: return 0
  config["proxy_pool"] = new
  save_config()  # 已有 atomic replace
```

注意：`proxy_manager._load_config` 走 `app_config.config`；写后内存 config 已更新，后续 `_pool_entries()` 自然变短。

### 4.4 单 proxy 字段

若仅配置 `config.proxy` 无 pool：`mark_dead` **不**清空 `proxy`（避免误杀唯一出口）；只记日志。

## 5. 测试

- `remove_dead_from_config` 删一条后 pool 少 1，文件可读回一致
- 中途写失败不损坏（mock save 抛错，内存 dead 仍在）
- 并发 mark 两次不同 URL，最终 pool 少 2
- 不存在的 URL 不写盘

## 6. 日志

`[!] proxy_pool 移除 dead (reason): http://… 剩余 N`
