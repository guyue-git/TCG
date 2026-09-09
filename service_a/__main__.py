"""服务 A HTTP 层与入口：python -m service_a [-c service_a_config.ini] [--replay].

接口：
* POST /messages   接收 monitor 推送（IncomingMessage JSON）
* GET  /health     健康检查（monitor 启动时探测）

标准库 http.server 实现，无新依赖；线程安全由 Dispatcher 保证。
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import load_service_config
from .dispatcher import Dispatcher
from .handler import HandlerContext
from .inbox import InboxStore
from .message import IncomingMessage

LOG = logging.getLogger(__name__)

# POST 请求体上限：真实消息 <1KB，1MB 已是百倍余量。
# 不设上限时，本机异常进程可用超大 Content-Length 把 JSON 全量读进内存。
MAX_BODY_BYTES = 1_000_000
# 413 前最多排空的字节数：排空少量超额体可让合法客户端收到 413 而非
# 连接被重置；再大的请求直接断开（客户端拿到错误即可，不必优雅）。
MAX_DRAIN_BYTES = 8 * 1024 * 1024


def build_dispatcher(cfg, base_dir: Path) -> Dispatcher:
    """组装 Dispatcher（供服务入口与测试复用）。"""
    ctx = HandlerContext(
        base_dir=base_dir,
        group_map=cfg.group_map,
        edge_path=cfg.edge_path,
        counterparty_name=cfg.counterparty_name,
        output_dirname=cfg.output_dirname,
    )
    return Dispatcher(ctx, InboxStore(base_dir / cfg.inbox_dir),
                      workers=cfg.workers)


def make_http_handler(dispatcher: Dispatcher, auth_token: str = ""):
    """构造 Handler 类（工厂便于测试注入 dispatcher）。"""

    class MessageHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默默认访问日志
            LOG.debug(fmt, *args)

        def _check_auth(self) -> bool:
            if not auth_token:
                return True
            supplied = self.headers.get("X-Auth-Token") or ""
            # compare_digest 防时序侧信道；本机回环面风险低，属零成本加固
            return hmac.compare_digest(supplied, auth_token)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._respond(200, {"status": "ok"})
            else:
                self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/messages":
                self._respond(404, {"error": "not found"})
                return
            if not self._check_auth():
                self._respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY_BYTES:
                    remaining = min(length, MAX_DRAIN_BYTES)
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 65536))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                    self.close_connection = True
                    self._respond(413, {"error": "payload too large"})
                    return
                payload = json.loads(
                    self.rfile.read(length).decode("utf-8"))
                message = IncomingMessage.from_dict(payload)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                self._respond(400, {"error": f"bad payload: {exc}"})
                return
            try:
                accepted, reason = dispatcher.submit(message)
            except Exception:  # noqa: BLE001 — 内部异常转 500，让 monitor 补投
                LOG.exception("submit 内部异常（group=%s seq=%s）",
                              message.group, message.seq)
                self._respond(500, {"error": "internal error"})
                return
            status = 202 if accepted else 200
            self._respond(status, {"accepted": accepted, "reason": reason})

        def _respond(self, code: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type",
                                 "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionAbortedError, OSError):
                pass  # 客户端提前断开：不影响服务

    return MessageHandler


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="service_a", description="消息处理中枢（服务 A）")
    parser.add_argument("-c", "--config", default="service_a_config.ini")
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--replay", action="store_true",
                        help="启动时重放 inbox 历史（幂等保护下补处理）")
    return parser.parse_args(argv)


def _force_utf8_stdio() -> None:
    """强制 stdio 为 UTF-8（源码/打包通用，不依赖环境变量）.

    stdout 接管道时 Python 默认按 ANSI 代码页（本机 GBK）写入，而 UI
    按 UTF-8 读取；实测 PyInstaller frozen exe 不理会 PYTHONIOENCODING
    （2026-09-04），故入口处直接 reconfigure。与 wechat_monitor 的
    同名函数保持一致（两进程为独立单元，刻意不共享模块）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout)
    args = _parse_args(argv)
    base_dir = Path(args.base_dir).resolve()
    cfg = load_service_config(base_dir / args.config, base_dir=base_dir)
    if not cfg.auth_token.strip():
        # 安全红线：/messages 一旦无鉴权，本机任意进程可注入伪造下单
        # 消息并产出带正式编号的确认书。ERROR 级会触发运维台告警。
        LOG.error("auth_token 未配置：/messages 当前无鉴权，本机任意进程"
                  "可注入伪造下单消息（请在 service_a_config.ini 配置"
                  " auth_token 并与 config.ini 的 service_a_token 一致）")
    dispatcher = build_dispatcher(cfg, base_dir)

    if args.replay:
        # 重放模式：不从 inbox 预填幂等集合（否则每条都被判 duplicate，
        # 重放恒为 0 条——2026-09-08 修复），靠 replay 过程中逐条占位去重
        n = dispatcher.replay()
        dispatcher.shutdown()
        LOG.info("重放完成，派发 %d 条", n)
        return 0

    # 幂等键从 inbox 落盘恢复：服务重启后重推/replay 不重复出 PDF
    dispatcher.seen_from_inbox()

    server = ThreadingHTTPServer(
        (cfg.host, cfg.port), make_http_handler(dispatcher, cfg.auth_token))
    LOG.info("服务 A 启动: http://%s:%d（workers=%d, inbox=%s）",
             cfg.host, cfg.port, cfg.workers, cfg.inbox_dir)
    # 运维管理台（ops_ui）以 CREATE_NEW_PROCESS_GROUP 启动本进程并以
    # CTRL_BREAK_EVENT 停止；SIGBREAK 默认行为是立即终止（跳过排空）。
    # 转为 KeyboardInterrupt 与 Ctrl+C 共用同一优雅收尾路径——注意不能在
    # handler 里直接调 server.shutdown()（与 serve_forever 同线程会死锁）。
    import signal as _signal

    def _on_sigbreak(signum, _frame):
        raise KeyboardInterrupt

    _signal.signal(_signal.SIGBREAK, _on_sigbreak)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("收到中断，排空队列后退出")
    finally:
        server.server_close()
        dispatcher.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
