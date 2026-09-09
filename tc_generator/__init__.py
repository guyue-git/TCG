"""tc_generator — 从微信 JSONL 日志生成 OTC 期权 Trade Confirmation (PDF).

一期范围：仅 CP001-9080 下单确认书。
数据来源：独立进程游标扫描 wechat_monitor.py 产出的 JSONL。
PDF 渲染：Jinja2 HTML 模板 + Edge headless（WeasyPrint 因本机缺 GTK DLL 降级）。
"""

__version__ = "0.1.0"
