"""处理器实现包：一种需求一个文件，在 HANDLERS 注册后即可被服务 A 派发."""

from __future__ import annotations

from . import cp001_90100_order, cp001_9080_order, cp003_order

# 注册表：类别名（classifier 输出）-> 处理器
HANDLERS = {
    cp001_9080_order.NAME: cp001_9080_order.handle,
    cp001_90100_order.NAME: cp001_90100_order.handle,
    cp003_order.NAME: cp003_order.handle,
}
