"""wechat_data —— 项目 B（alert）内嵌的微信数据直连子模块。

将 WeChatDataAnalysis（项目 A）的核心能力移植为独立子模块：

* detector.py       微信安装/数据目录/当前登录账号检测
* key_extractor.py  从运行中的微信进程内存提取 SQLCipher 密钥
* decryptor.py      SQLCipher 4 逐页解密
* snapshot.py       解密快照缓存（mtime 增量刷新）
* reader.py         会话/消息读取（字段对齐 wechat_monitor 归一化）
* account_guard.py  「登录实例 ↔ 数据」绑定链与巡检
* provider.py       对 wechat_monitor 暴露与远程 API 等价的本地接口

设计约束（铁律）：
* 所有路径相对 base_dir（frozen 态 = exe 所在目录）锚定，不依赖 cwd；
* 不依赖项目 A 的任何进程 / HTTP API / native 组件；
* 密钥只落盘在 runtime/ 下并按账号隔离，读取数据前必须完成
  「进程 → 密钥 → 库文件 → 登录账号」四点绑定。
"""

from __future__ import annotations

__all__ = [
    "paths",
    "winproc",
    "sqlcipher_spec",
    "key_extractor",
    "detector",
    "decryptor",
    "snapshot",
    "reader",
    "account_guard",
    "provider",
]
