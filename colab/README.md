# Google Colab 无代理注册（小白版）

**不改仓库现有业务代码**，只使用本目录新增脚本。

Colab 跑在 Google 机器上，**出口就是 Google 机房 IP**，更容易被 xAI 打 `bot_flag`。本方案适合：你想换一批「非自家住宅代理」的出口做实验；**不保证**比住宅代理更干净。

## 你需要准备什么

1. Google 账号（能打开 [Google Colab](https://colab.research.google.com/)）
2. 一份能用的 `config.json`（邮箱 API、chenyme 等按你本地一样填）
3. 本仓库源码（GitHub 公开仓可直接 clone；私有仓用上传 zip）

## 硬限制（必读）

| 点 | 说明 |
|----|------|
| **不能真正「无感自动换机继续跑」** | Colab 没有官方「换 VM 并恢复同一会话」API |
| **「切换宿主」怎么做** | 脚本可调用 `runtime.unassign()` **断开当前 Runtime**；你重新连接后通常会分到**另一台机器/新 IP**，再点一次运行 |
| **无代理** | 脚本强制清空 `proxy` / `proxy_pool`，直连 Colab IP |
| **浏览器** | 必须 headless + no-sandbox（本进程 monkeypatch，不改源码） |
| **空闲断连** | 免费 Colab 闲置会断，大批量请分段跑 |

## 最快路径（推荐 Notebook）

1. 打开 Colab → **上传** `colab/grok_register_colab.ipynb` 并打开  
2. 按单元从上到下运行（安装依赖 → 拉代码/上传 config → 注册）  
3. 结束下载 `accounts_*.txt`、`cpa_auths/`、`grok2api_build_import.json`（若开启 CPA）

## 命令行等价（在已 clone 的仓库根目录）

```bash
# 装系统依赖（Chrome 相关，见 notebook 第 1 格）
# 务必带 PYTHONPATH，否则会 No module named app_config
cd /content/grok-register   # 或你的 clone 路径
export PYTHONPATH=/content/grok-register
python colab/run_colab_register.py --count 3

# 每 2 个号尝试 unassign Runtime 换机（断后需手动重连再跑）
python colab/run_colab_register.py --count 6 --rotate-after 2

# 若启动时就是机房 ASN，直接请求换机
python colab/run_colab_register.py --count 1 --rotate-if-hosting
```

### 若报 `No module named 'app_config'`

1. 确认 clone 的是 **含 `colab/` 的完整仓库**（推荐 `linG5821/grok-register`）
2. 在 Colab 执行：
   ```bash
   %cd /content
   !rm -rf /content/grok-register
   !git clone --depth 1 -b main https://github.com/linG5821/grok-register.git /content/grok-register
   !cd /content/grok-register && PYTHONPATH=/content/grok-register python colab/run_colab_register.py --count 1
   ```

### 若报 `getcwd: cannot access parent directories` / `FileNotFoundError` in pip

说明 **当前 notebook 工作目录已被删掉**（例如先 `rm -rf /content/grok-register` 而 kernel 还停在里面）。

**立刻修复（先跑这一格）：**
```python
%cd /content
```
然后再跑安装依赖 / clone。  
**不要**在「当前目录就是 grok-register」时执行 `!rm -rf /content/grok-register`；应先 `%cd /content` 再删。

## 默认策略

- **关闭** chenyme `convert-to-build`（远程 convert 叠机房 IP 更易 bot）  
  需要远程 convert 时加：`--keep-chenyme-convert`
- CPA 跟随你的 `config.json`；可 `--enable-cpa` / `--disable-cpa` 覆盖
- 强制：`proxy=""`、`proxy_pool=[]`、`proxy_health_enabled=false`

## 换机操作（手动，最稳）

1. 菜单 **Runtime → Disconnect and delete runtime**  
2. **Runtime → Run all**（或重新连接后只跑注册格）  
3. 看日志里的 `出口 IP=` 是否变了  

脚本里的 `unassign` 等价于「请会话滚蛋」，**不会**在断线后自动接着跑下一批。

## 常见失败

| 现象 | 处理 |
|------|------|
| Chrome/Chromium 起不来 / `browser executable file path cannot be found` | 重跑安装格（会装 **google-chrome-stable**）；确认 `!which google-chrome-stable` 有输出；入口会 `set_browser_path` |
| Turnstile / 资料页卡住 | Colab 机房 IP 风控；换 Runtime 或改回本地住宅代理 |
| Build 403 / bot_flag | 与出口 ASN 相关；换机后新号再试，已 flag 号无法救 |
| 邮箱 API 失败 | `config.json` 里邮箱密钥/域名是否填对；Colab 能否访问你的邮箱服务 |

## 文件说明

| 文件 | 作用 |
|------|------|
| `grok_register_colab.ipynb` | 小白一键：安装 → 配置 → 跑 → 打包下载 |
| `run_colab_register.py` | 无代理 + headless 补丁 + 可选换机请求 |
| `README.md` | 本说明 |
