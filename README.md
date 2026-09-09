# alert —— OTC 期权交易确认书（Trade Confirmation）自动化

微信群消息 → 自动解析 → 生成交易确认书 PDF。三段式进程链：
`monitor`（采集）→ `service_a`（分类派发）→ `tc_generator`（渲染 PDF）。

## 目录结构

| 路径 | 说明 |
|---|---|
| `wechat_monitor.py` | 监控采集（local 直连本机微信 / remote 轮询后端 API） |
| `service_a/` | 消息处理中枢：鉴权、幂等、分类、派发 |
| `tc_generator/` | 确认书构建与渲染（Jinja2 模板 + HTML → PDF） |
| `wechat_data/` | 本机微信数据直连子模块（账号绑定、密钥提取、解密快照、读取） |
| `ops_ui/` | 运维台 UI（Tkinter + pystray，管理器两进程） |
| `tests/` | 单元 / 集成 / 冒烟测试 |
| `packaging/` | PyInstaller onedir 打包脚本与 spec |

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # 依赖见下
cp config.example.ini          config.ini
cp service_a_config.example.ini service_a_config.ini
# 填写 config.ini：target_groups（监控群名）、service_a_token
# 填写 service_a_config.ini：auth_token（与上面一致）、[groups] 群名→对手方映射
```

令牌生成（两份配置必须一致）：

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

## 依赖

`requests`、`jinja2`、`pystray`、`pillow`、`zstandard`、`chinese_calendar`、`lunar-python` /
`lunardate`、`cryptography`、`PyInstaller`（仅打包）。

## 运行

- **开发态**：分别启动 `python entry_service_a.py` 与 `python wechat_monitor.py -c config.ini`
- **运维台**：`python entry_ops_ui.py`
- **打包产物**：双击 `tc_ops_ui.exe` 点「启动」（UI 自动先服务 A → /health → monitor）
- **local 模式首次绑定需以管理员身份运行**（从微信进程内存提取数据库密钥，之后走缓存）

## 打包

```bash
.venv/Scripts/python.exe packaging/build_alert.py <产物名>
```

产物为 onedir：`monitor.exe` / `service_a.exe` / `tc_ops_ui.exe` + 共享 `_internal` +
`chrome-headless-shell`。分发 = **整目录**拷贝（含 270MB 内核），不能只拷 exe。

## 渲染内核（chrome-headless-shell）

打包时从 `packaging/chrome-headless-shell/` 拷入产物目录（该目录不入库，见 `.gitignore`）。
获取方式：Chrome for Testing 官方分发下载对应平台与版本的 `chrome-headless-shell` 压缩包，
解压后使可执行文件位于 `packaging/chrome-headless-shell/chrome-headless-shell.exe`。
未放置时可留空 `edge_path`，程序会回退到系统 Edge。

## 不入库的文件

运行时数据（`runtime/` 密钥缓存、`wechat_logs/`、`service_a_inbox/`、`output/`、
`.monitor_state.json` 水位）、真实配置（含鉴权令牌）、构建产物与 270MB 渲染内核，
均已由 `.gitignore` 排除。
