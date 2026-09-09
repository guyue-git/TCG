"""处理器协议与注册表.

新需求接入方式（架构裁决：一种需求一个独立文件）：
1. 在 handlers/ 下新建 <name>.py，实现 Handler 协议（handle 方法）；
2. 在 handlers/__init__.py 的 HANDLERS 注册表加一行 {name: handler}；
3. 分类器 classifier.py 增加类别 → 处理器名的映射。

Handler.handle 收到的 ctx 携带服务级依赖（配置、群映射、幂等集合），
处理器自身保持无全局状态，便于测试与并行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .message import IncomingMessage


@dataclass
class HandlerContext:
    """传给处理器的服务级上下文（共享依赖集中在 ctx，不用全局变量）。"""

    base_dir: Path                      # 项目根目录
    group_map: dict[str, str]           # 群名 → 对手方编号
    edge_path: str                      # Edge 可执行文件路径
    counterparty_name: str = "COUNTERPARTY"
    output_dirname: str = "output"      # PDF 产出目录（base_dir 下）
    extra: dict = field(default_factory=dict)


class Handler(Protocol):
    """处理器协议：类别名 -> 可调用。"""

    def handle(self, message: IncomingMessage, ctx: HandlerContext) -> Path | None:
        """处理一条消息；返回产出文件路径，无产出返回 None。

        抛出的异常由分发器捕获记日志，不影响线程池其他任务。
        """
        ...


# 注册表：类别名 -> 处理器可调用。由 handlers/__init__.py 在导入时填充。
REGISTRY: dict[str, Callable[..., Path | None]] = {}
