# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单 spec 三入口构建（方案 B：onedir 共享运行时）.

产物（dist/alert/）：
    monitor.exe     消息采集（wechat_monitor）
    service_a.exe   消息处理中枢（service_a）
    tc_ops_ui.exe   运维管理台（ops_ui，windowed）
    _internal/      共享运行库 + tc_generator 模板与日历数据

渲染内核（2026-09-08 裁决，方案 A 产物根分发）：不进 spec——PyInstaller
onedir 的 COLLECT 会把所有 TOC 目标强制放入 _internal，无法表达「产物根」。
由 packaging/build_alert.py 在 PyInstaller 成功后整目录复制
chrome-headless-shell/ 到 dist/alert/（约 270MB，含 dll/pak/locales，
必须保持结构）。请统一经该脚本构建，勿直接调用 PyInstaller。

三个 Analysis 各自独立（无相互 import），COLLECT 合并去重共享库；
tc_generator 的 templates/data 以 <_MEIPASS>/tc_generator/ 布局解包，
与 tc_generator/resources.package_resource 的 frozen 读取路径对应。
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# spec 位于 packaging/ 子目录，项目根取 SPECPATH 的上一级
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "tc_generator/templates"), "tc_generator/templates"),
    (os.path.join(ROOT, "tc_generator/data"), "tc_generator/data"),
    # chinese_calendar 自带节假日数据文件（2027-2030 预设之外年份仍需）
    *collect_data_files("chinese_calendar"),
]

hiddenimports = [
    "chinese_calendar",
    "lunardate",
    "jinja2",
    # wechat_data 直连子模块（monitor local 数据源，2026-09-08）：
    # build_client 内为函数级惰性导入，显式列出防 modulegraph 漏收
    "wechat_data",
    "wechat_data.provider",
    "wechat_data.account_guard",
    "wechat_data.detector",
    "wechat_data.key_extractor",
    "wechat_data.decryptor",
    "wechat_data.snapshot",
    "wechat_data.reader",
    "wechat_data.paths",
    "wechat_data.winproc",
    "wechat_data.sqlcipher_spec",
    # wx_key Hook 后备（cp313 扩展模块，vendor wheel 已装入 .venv）
    "wx_key",
    "wx_key.wx_key",
    # reader 消息内容 zstd 解压（微信 4.x 把部分消息的压缩字节直接写进
    # message_content，缺依赖时该类消息静默降级为空，2026-09-09 D3）
    "zstandard",
]

a_service = Analysis(
    [os.path.join(ROOT, "entry_service_a.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

a_monitor = Analysis(
    [os.path.join(ROOT, "wechat_monitor.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

a_ui = Analysis(
    [os.path.join(ROOT, "entry_ops_ui.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz_service = PYZ(a_service.pure)
pyz_monitor = PYZ(a_monitor.pure)
pyz_ui = PYZ(a_ui.pure)

exe_service = EXE(
    pyz_service,
    a_service.scripts,
    [],
    exclude_binaries=True,
    name="service_a",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

exe_monitor = EXE(
    pyz_monitor,
    a_monitor.scripts,
    [],
    exclude_binaries=True,
    name="monitor",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

exe_ui = EXE(
    pyz_ui,
    a_ui.scripts,
    [],
    exclude_binaries=True,
    name="tc_ops_ui",
    debug=False,
    strip=False,
    upx=False,
    console=False,      # 窗口化：日志经管道进 UI 窗口，不留黑窗
)

coll = COLLECT(
    exe_service,
    exe_monitor,
    exe_ui,
    a_service.binaries,
    a_service.datas,
    a_monitor.binaries,
    a_monitor.datas,
    a_ui.binaries,
    a_ui.datas,
    strip=False,
    upx=False,
    name="alert",
)
