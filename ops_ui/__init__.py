"""运维管理台（ops_ui）进程管控层.

管理两个受管进程（方案 §7.1-7.2）：
* 服务 A：python -m service_a（先启动，/health 就绪后再启动 monitor）
* monitor：python wechat_monitor.py（stdin 管道写入启动模式选择）

边界（用户裁决）：微信登录与 WeChatDataAnalysis 不归本层管理。
"""
