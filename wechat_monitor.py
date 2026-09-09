#!/usr/bin/env python3
"""
监控带有指定标记（默认为 '#'）前缀的微信群消息。

支持两种数据源（config.ini [data_source] source）：

* local  —— 内嵌 wechat_data 子模块直连本机微信：自动检测登录账号、
  从微信进程内存提取密钥、解密快照、本地读取会话/消息。
  完全不依赖 WeChatDataAnalysis（项目 A），单程序独立运行。
* remote —— 轮询 WeChatDataAnalysis 桌面应用的本地 HTTP API
  （默认 http://127.0.0.1:10392），保持向后兼容。
* auto   —— 优先尝试 local；本机微信不可用（未安装/未登录）时回退 remote。

匹配到的群消息追加写入 JSONL 文件。仅记录新消息：只记录 ID/序号大于
上次已处理值的消息，水位线持久化，重启不漏消息。

local 模式的账号安全：每轮轮询前执行「登录实例 ↔ 数据」绑定巡检
（wechat_data.account_guard），微信切换账号时自动重绑；水位线按
<账号>|<会话> 隔离，绝不跨账号复用。
"""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import hashlib
import queue
import requests

LOG = logging.getLogger("monitor_hashtag") # 创建日志记录器实例 

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10392
DEFAULT_POLL_INTERVAL = 2.0                # 默认轮询间隔（秒）
DEFAULT_PREFIX = "#"                       # 默认消息前缀标记
DEFAULT_PAGE_LIMIT = 50                    # 每次拉取消息的默认条数上限
DEFAULT_STATE_FILE = ".monitor_state.json" # 默认状态文件名，记录已读取到的水位线
APP_NAME = "WeChatDataAnalysis"            # 关联的后端应用名

# Safety cap on pages fetched per chat per poll cycle, so a very large backlog
# after downtime is drained over several cycles instead of stalling the loop.
# Worst case per cycle per chat: DEFAULT_PAGE_LIMIT * MAX_INCREMENT_PAGES.
MAX_INCREMENT_PAGES = 20                   # 轮询周期内每个聊天获取的页数

# Unix 时间戳归一化
TS_MILLIS_THRESHOLD = 10**12
TS_MICROS_THRESHOLD = 10**15

# Windows 保留的设备名称永远不能用作文件名
RESERVED_FILENAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)
MAX_FILENAME_PART_LEN = 80

# 服务 A 推送的超时与降级告警间隔（秒）：A 离线时按此间隔打 WARNING，不刷屏
PUSH_TIMEOUT = 3.0

# E2 修复：首触补采的时钟容差（秒）。create_time 为微信服务端时间，与本地
# monitor 启动时刻比较时放宽该容差，避免时钟偏移导致待救消息被误判为积压。
FIRST_CONTACT_RESCUE_GRACE = 120.0

# E1 修复：目标群未解析的告警限频（秒）。群会话按活跃度维护，不活跃群可能
# 长期解析不到，按此间隔提醒，不刷屏。
MISSING_GROUP_WARN_INTERVAL = 300.0
PUSH_DEGRADE_LOG_INTERVAL = 60.0
# 推送失败的退避重试间隔（秒）：首试 + len(PUSH_RETRY_DELAYS) 次重试，
# 全部失败后落盘 pending 文件（待补投），绝不静默丢消息（2026-09-04 实测缺口：
# 服务 A 短暂离线时单次失败即丢弃，确认书永久丢失且水位线已推进无法自愈）
PUSH_RETRY_DELAYS: Tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)
# 待补投队列的周期性补投间隔（秒）：服务 A 恢复后自动追赶，无需重启 monitor
PENDING_RETRY_INTERVAL = 30.0


class ServiceAPusher:
    """把消息文本拼接后 POST 到服务 A（可选，config 配 service_a_url 启用）.

    设计：
    * 只送文本与最小元数据，业务解析完全在服务 A 侧（解耦）；
    * 单调 seq 保证服务 A 幂等与按序；
    * 服务 A 离线/失败：退避重试 → 仍失败落盘 pending 文件 → 周期性自动补投，
      重启时 pending 重新入队。服务 A 按 (group, msg_id) 幂等，重复投递无害；
      绝不影响 JSONL 采集主链路（优雅降级）；
    * 后台单线程队列发送，不阻塞轮询循环。
    """

    def __init__(self, url: str, token: str = "",
                 pending_path: Optional[Path] = None):
        self.url = url.rstrip("/")
        self.token = token
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        self._stop = threading.Event()
        self._last_degrade_log = 0.0
        self._last_pending_attempt = 0.0
        self._pending_path = pending_path or Path("pending_push.jsonl")
        self._pending: List[Dict[str, Any]] = []   # 未送达消息（worker 单线程访问）
        self._load_pending()
        self._worker = threading.Thread(
            target=self._run, name="service-a-pusher", daemon=True)
        self._worker.start()

    def push(self, *, group: str, sender: Optional[str], msg_id: Optional[int],
             create_time: Optional[int], content: str,
             quoted_content: str = "", quoted_msg_id: Optional[int] = None) -> None:
        """拼接文本并投递发送队列（非阻塞）。"""
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
        payload = {
            "seq": seq,
            "group": group,
            "sender": sender,
            "msg_id": msg_id,
            "create_time": create_time,
            "content": content or "",
            "quoted_content": quoted_content or "",
            "quoted_msg_id": quoted_msg_id,
        }
        self._queue.put(payload)

    def _run(self) -> None:
        import urllib.request
        import urllib.error
        import json as _json
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._retry_pending()
                continue
            if payload is None:
                break
            self._deliver(payload)

    def _post_once(self, payload: Dict[str, Any]) -> bool:
        """单次投递尝试。返回 True 表示服务 A 已收到（含 duplicate 判定）。"""
        import urllib.request
        import urllib.error
        import json as _json
        data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/messages", data=data,
            headers={"Content-Type": "application/json; charset=utf-8",
                     **({"X-Auth-Token": self.token} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT) as resp:
                # 2xx 但 accepted=false：服务 A 判 duplicate——消息其实已在
                # 服务 A，按投递成功处理，不重试不落盘（幂等键兜底）
                body = _json.loads(resp.read().decode("utf-8") or "{}")
                if isinstance(body, dict) and not body.get("accepted", True):
                    LOG.warning(
                        "服务 A 判定消息重复（seq=%s msg_id=%s group=%r）"
                        "——若为 monitor 重启后的真实新消息，请检查幂等键",
                        payload.get("seq"), payload.get("msg_id"),
                        payload.get("group"))
                return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            now = time.monotonic()
            if now - self._last_degrade_log > PUSH_DEGRADE_LOG_INTERVAL:
                self._last_degrade_log = now
                LOG.warning("服务 A 推送失败（将退避重试）: %s", exc)
            return False

    def _deliver(self, payload: Dict[str, Any]) -> bool:
        """投递一条消息：失败按退避重试，最终失败落盘待补投。"""
        for attempt in range(len(PUSH_RETRY_DELAYS) + 1):
            if self._post_once(payload):
                if attempt:
                    LOG.info("服务 A 重试投递成功（seq=%s msg_id=%s group=%r，"
                             "第 %d 次尝试）", payload.get("seq"),
                             payload.get("msg_id"), payload.get("group"),
                             attempt + 1)
                return True
            if attempt >= len(PUSH_RETRY_DELAYS):
                break
            if self._stop.wait(PUSH_RETRY_DELAYS[attempt]):
                break  # 停机中：不再重试，落盘待补投
        self._add_pending(payload)
        return False

    def _retry_pending(self) -> None:
        """周期性补投未送达消息（服务 A 恢复后自动追赶，无需重启）。"""
        if not self._pending:
            return
        now = time.monotonic()
        if now - self._last_pending_attempt < PENDING_RETRY_INTERVAL:
            return
        self._last_pending_attempt = now
        still: List[Dict[str, Any]] = []
        for payload in self._pending:
            if self._stop.is_set() or not self._post_once(payload):
                still.append(payload)
            else:
                LOG.info("待补投消息已追补至服务 A（seq=%s msg_id=%s group=%r）",
                         payload.get("seq"), payload.get("msg_id"),
                         payload.get("group"))
        if len(still) != len(self._pending):
            self._pending = still
            self._persist_pending()

    # ---- 待补投持久化（worker 单线程访问，无需加锁） ----

    def _load_pending(self) -> None:
        """启动时加载 pending 文件并重新入队（崩溃/停机前未送达的消息）。"""
        path = self._pending_path
        if not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    LOG.warning("pending 文件存在损坏行，已跳过: %r", line[:80])
                    continue
                if isinstance(payload, dict):
                    self._pending.append(payload)
                    self._queue.put(payload)
        except OSError as exc:
            LOG.warning("pending 文件读取失败 %s: %s", path, exc)
        if self._pending:
            LOG.warning("从 %s 恢复 %d 条未送达消息，开始补投",
                        path, len(self._pending))

    def _add_pending(self, payload: Dict[str, Any]) -> None:
        self._pending.append(payload)
        LOG.warning(
            "服务 A 推送最终失败，已落盘待补投（%s）: seq=%s msg_id=%s group=%r"
            "——服务 A 恢复后将自动补投，也可重启 monitor 触发",
            self._pending_path, payload.get("seq"), payload.get("msg_id"),
            payload.get("group"))
        self._persist_pending()

    def _persist_pending(self) -> None:
        """把待补投列表原子写回文件（tmp + replace，防半行）；清空则删除。"""
        tmp = self._pending_path.parent / (self._pending_path.name + ".tmp")
        try:
            if not self._pending:
                if self._pending_path.exists():
                    self._pending_path.unlink()
                return
            self._pending_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                for payload in self._pending:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            tmp.replace(self._pending_path)
        except OSError as exc:
            LOG.error("待补投文件写入失败 %s: %s", self._pending_path, exc)

    def close(self) -> None:
        self._stop.set()
        self._queue.put(None)
        self._worker.join(timeout=3.0)
        # 排空 worker 未取走的消息，落盘待补投（下次启动自动补投；
        # 若 worker 恰好也在处理同一条，pending 最多重复一行，
        # 服务 A 按 (group, msg_id) 幂等，无害）
        while True:
            try:
                payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if payload is not None:
                self._add_pending(payload)

# 候选原始字段名（按优先级排序），用于兼容后端不同版本的字段命名漂移
CANDIDATE_KEYS = {
    "msg_id": ["localId", "serverId", "local_id", "server_id", "id", "serverIdStr", "rowid"],
    "content": ["message_content", "content", "compress_content", "text"],
    "sender": ["senderDisplayName", "senderUsername", "sender_display_name", "sender", "real_sender_id", "talker"],
    "create_time": ["create_time", "time", "timestamp", "createTime"],
    "is_self": ["computed_is_send", "isSent", "is_self", "isSend"],
    "is_group": ["isGroup", "is_group"],
    "name": ["name", "nickname", "display_name"],
    "username": ["username", "userName", "talker", "wxid"],
}


@dataclass # config配置
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    target_groups: List[str] = None              # 监控群组列表
    output_dir: str = "wechat_logs"              # 日志目录
    prefix: str = DEFAULT_PREFIX                 # 前缀符号
    poll_interval: float = DEFAULT_POLL_INTERVAL # 轮询间隔
    page_limit: int = DEFAULT_PAGE_LIMIT         # 拉取消息每页限制
    account: str = ""                            # 微信账号
    state_file: str = DEFAULT_STATE_FILE         # 状态文件
    source: str = "auto"
    start_mode: str = "resume"                   # "resume"断点续传 | "fresh"重新刷新
    quote_marker: str = "/"                      # 引用消息标识
    backtrack_count: int = 0                     # 回溯条数
    service_a_url: str = ""                      # 服务 A 地址（空=不推送）
    service_a_token: str = ""                    # 服务 A 鉴权令牌（可选）
    # ---- 数据源（local=内嵌直连微信 | remote=项目 A API | auto）----
    data_source: str = "local"
    db_storage_path: str = ""                    # 显式指定微信数据根目录（可选）
    snapshot_dir: str = ""                       # 解密快照目录（可选，默认 runtime/）

    def api_base(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass # 归一化后的消息数据类
class NormalizedMessage:
    msg_id: Optional[int]      # 消息序号
    group_username: str        # 群id
    group_name: str            # 群名
    sender: Optional[str]      # 发送者
    content: str               # 消息内容
    create_time: Optional[int] # 消息时间
    is_self: Optional[bool]


# 依次遍历候选字段名，返回第一个命中且非空的值；若全部未命中则返回 None
def _first(d: Dict[str, Any], keys: List[str]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


# 将值转换为整数，失败或空值返回 None
def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


# 将值转换为布尔，失败或空值返回 None
def _to_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


# 将时间统一归一化为秒级 Unix 时间戳
def _to_timestamp(value: Any) -> Optional[int]:
    num = _to_int(value)
    if num is None:
        return None
    if num > TS_MICROS_THRESHOLD:
        return num // 1_000_000
    if num > TS_MILLIS_THRESHOLD:
        return num // 1000
    return num


# 归一化字段
def extract_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the fields we care about using candidate-key fallbacks."""
    f = {
        "msg_id": _first(raw, CANDIDATE_KEYS["msg_id"]),
        "content": _first(raw, CANDIDATE_KEYS["content"]),
        "sender": _first(raw, CANDIDATE_KEYS["sender"]),
        "create_time": _first(raw, CANDIDATE_KEYS["create_time"]),
        "is_self": _first(raw, CANDIDATE_KEYS["is_self"]),
    }
    # senderDisplayName can be the literal placeholder "If None" for self-sent
    # messages; fall back to the stable senderUsername (wxid) in that case.
    if not f["sender"] or str(f["sender"]).strip() in ("", "If None", "None", "null"):
        f["sender"] = _first(raw, ["senderUsername", "sender_username", "sender"])
    return f


# 返回消息序号，时间戳
def seq_of(raw: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """Return (monotonic_seq, create_time_sec) used as the dedup cursor."""
    f = extract_fields(raw)
    return _to_int(f["msg_id"]), _to_timestamp(f["create_time"])


# 判断是否是目标消息
def is_target_message(raw: Dict[str, Any], prefix: str) -> bool:
    """True if this is a text message whose content starts with the prefix."""
    content = _first(raw, CANDIDATE_KEYS["content"])
    if not isinstance(content, str):
        return False
    return content.lstrip().startswith(prefix)


# 返回被引用的消息
def extract_quote(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
       依据quoteServerId判断是否为引用消息。
       如果被引用的原始消息已被召回（recall），那么服务器可能返回：quoteServerId 为字符串 "null" 或直接缺失；
       但 quoteContent 依然保留了引用时的预览文本（即召回后仍能看到“原消息已被撤回”之类的占位符）。
       此时，只要 quoteServerId 有效或 quoteContent 非空，函数仍然保留这条引用，而不是直接丢弃。
    """
    qid = _to_int(_first(raw, ["quoteServerId", "quote_server_id"]))
    qcontent = _first(raw, ["quoteContent", "quote_content"])
    if qcontent in (None, "", "If None", "None", "null"):
        qcontent = ""
    # Keep the quote only when there is something to persist: a real id or text.
    if qid is None and not qcontent:
        return None
    return {
        "quote_server_id": qid,   # None when the original was recalled
        "quote_username": _first(raw, ["quoteUsername", "quote_username"]),
        "quote_content": qcontent,
        "quote_type": _first(raw, ["quoteType", "quote_type"]),
    }


# 判断是否是新消息
def is_newer(seq: Optional[int], ctime: Optional[int],
             last_seq: Optional[int], last_time: Optional[int]) -> bool:
    if seq is not None:
        if last_seq is None:
            return True
        if seq > last_seq:
            return True
        if seq == last_seq:
            # same seq, treat later timestamp as newer
            return ctime is not None and last_time is not None and ctime > last_time
        return False
    # no seq available: fall back to create_time
    if ctime is None:
        return False
    if last_time is None:
        return True
    return ctime > last_time


class ApiClient: # 与后端程序通信类
    def __init__(self, cfg: Config, timeout: float = 30.0):
        # timeout=30：/api/chat/messages 后端单请求固有延迟实测 5-8s（2026-09-07），
        # 10s 余量不足——系统负载峰值（如 Edge 渲染风暴）会顶过 10s 触发 poll error
        self.cfg = cfg
        self.timeout = timeout
        # 复用会话连接
        self.session = requests.Session()

    # 拼接url
    def _url(self, path: str) -> str:
        return f"{self.cfg.api_base()}{path}"

    # get请求
    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            resp = self.session.get(self._url(path), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"request to {path} failed: {exc}") from exc
        if resp.status_code != 200:
            raise ApiError(f"{path} returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(f"{path} returned non-JSON body") from exc

    # 是否存活
    def health(self) -> bool:
        try:
            self.get_json("/api/health")
            return True
        except ApiError:
            return False

    # 轮询等待后端就绪
    def wait_health(self, retries: int = 30, delay: float = 2.0) -> None:
        for i in range(retries):
            if self.health():
                LOG.info("backend health OK (%s)", self.cfg.api_base())
                return
            LOG.info("backend not ready, retry %d/%d ...", i + 1, retries)
            time.sleep(delay)
        raise ApiError(f"backend at {self.cfg.api_base()} not reachable. "
                      f"Start {APP_NAME} exe and ensure WeChat is logged in.")

    # 获取会话列表
    def list_sessions(self, account: str) -> List[Dict[str, Any]]:
        data = self.get_json("/api/chat/sessions", params={
            "account": account,
            "limit": 400,
            "include_hidden": "false",
            "include_official": "false",
            "source": "auto",
        })
        return data.get("sessions", []) if isinstance(data, dict) else []

    # 获取某个会话的消息列表
    def list_messages(self, account: str, username: str, limit: int,
                      offset: int = 0, order: str = "desc",
                      source: str = "auto") -> List[Dict[str, Any]]:
        data = self.get_json("/api/chat/messages", params={
            "account": account,
            "username": username,
            "limit": limit,
            "offset": offset,
            "order": order,
            "source": source,
        })
        return data.get("messages", []) if isinstance(data, dict) else []

    # 自动查找返回第一个可用的账号
    def discover_account(self) -> str:
        for path in ("/api/accounts", "/api/chat/accounts"):
            try:
                data = self.get_json(path)
            except ApiError:
                continue
            if not isinstance(data, dict):
                continue
            accounts = data.get("accounts")
            if not isinstance(accounts, list) or not accounts:
                continue
            first = accounts[0]
            if isinstance(first, str):
                LOG.info("auto-discovered account: %s", first)
                return first
            if isinstance(first, dict):
                aid = first.get("id") or first.get("username") or first.get("account")
                if aid:
                    LOG.info("auto-discovered account: %s", aid)
                    return str(aid)
        return ""


class ApiError(RuntimeError):
    pass


# 确定账号，建议多账号时直接指定
def _resolve_account(cfg: Config, client: ApiClient) -> str:
    """Return an explicit account if configured, else try auto-discovery.

    Falls back to an empty string (which the backend accepts when only one
    account is loaded, as proven by probe.py) so a failed/odd discovery never
    aborts startup.
    """
    if cfg.account:
        return cfg.account
    try:
        return client.discover_account() or ""
    except ApiError as exc:
        LOG.warning("account auto-discovery failed (%s); using empty account", exc)
        return ""


class Monitor: # 监控轮询类
    def __init__(self, client: ApiClient, cfg: Config, account: str,
                 output_dir: Optional[Path] = None, state_path: Optional[Path] = None):
        self.client = client                                 # 后端客户端
        self.cfg = cfg
        self.account = account
        self.output_dir = output_dir or Path(cfg.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_path or Path(cfg.state_file)
        self.state: Dict[str, Any] = self._load_state()      # 水位线，记录已处理到哪条消息
        if self.cfg.start_mode == "fresh":
            LOG.info("fresh start requested; discarding previous watermark")
            self.state = {}
        self._stop = threading.Event()
        self._started_at = time.time()                       # E2 首触补采基准
        self._missing_warned_at: Dict[str, float] = {}       # E1 未解析群告警限频
        self._resolved: Dict[str, str] = {}                  # group_name -> username
        self._seen_ids: Dict[str, Set[int]] = {}             # group_name -> msg_ids 已处理消息序号
        self._seen_quotes: Dict[str, Set[Tuple[Optional[int], Optional[int]]]] = {}
        # group_name -> (quoted_id, quoted_from) 引用记录去重键：
        # 业务主键是 "(被引用消息, 引用者)" 二元组，同一条历史消息被不同
        # 消息引用属于独立事件，各需落盘一次。
        self._page_offsets: Dict[str, int] = {}              # username -> next desc 下次轮询起始页
        # 服务 A 推送器（可选）：配置 service_a_url 时启用；
        # 未送达消息落盘到输出目录的 pending 文件，恢复后自动补投
        self.pusher: Optional[ServiceAPusher] = (
            ServiceAPusher(cfg.service_a_url, cfg.service_a_token,
                           pending_path=self.output_dir / "pending_push.jsonl")
            if cfg.service_a_url else None)
        if self.pusher:
            LOG.info("service A push enabled -> %s", cfg.service_a_url)
        else:
            LOG.warning(
                "service_a_url 未配置：仅本地归档（wechat_logs/），不推送"
                "服务 A，不会产出确认书 PDF。若预期出 PDF，请检查 config.ini"
                " 的 [monitor] 节（注意：该键必须留在 [monitor] 节内，"
                "2026-09-09 曾因落入 [data_source] 节被静默忽略）")

    # 从文件读取上次的水位线
    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                LOG.warning("state file %s unreadable, starting fresh", self.state_path)
        return {}

    # 记录水位线
    def _save_state(self) -> None:
        """原子落盘：tmp + os.replace（write_text 直写在断电/强杀时可能
        撕裂 → 重启走 "starting fresh" → 停机期间积压消息被基线跳过，
        静默丢失）。与 ServiceAPusher._persist_pending 同一范式。"""
        tmp_path = self.state_path.with_suffix(
            self.state_path.suffix + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.write(json.dumps(self.state, ensure_ascii=False))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.state_path)
        except OSError as exc:
            LOG.warning("failed to save state: %s", exc)

    # 预加载已处理的消息序号与引用键对
    def _load_seen_ids(self, group_name: str) -> Set[int]:
        """Pre-load dedup state for this group from its JSONL files.

        After a crash the monitor replays from the last *saved* watermark, so
        records written right before the crash would otherwise be written a
        second time. Loading the ids of everything already on disk makes the
        replay idempotent for messages that expose an id (messages without an
        id cannot be deduped and fall back to at-least-once semantics).

        Two collections are populated:

        * ``_seen_ids[group_name]`` -- msg_id of every record on disk, used to
          dedup direct (native) messages by their own id.
        * ``_seen_quotes[group_name]`` -- ``(quoted_id, quoted_from)`` pairs of
          every quoted record on disk, used to dedup quote events. A quoted
          record's ``msg_id`` is the *referenced* message's id, so keying on
          id alone would wrongly suppress a second, independent quote of the
          same message (fix: dedup quotes by "who quoted what").
        """
        seen: Set[int] = set()
        quotes: Set[Tuple[Optional[int], Optional[int]]] = set()
        store_key = self._store_key(group_name)
        prefix = self._record_prefix(group_name)
        for path in self.output_dir.glob(f"{prefix}_*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(rec, dict):
                            mid = _to_int(rec.get("msg_id"))
                            if mid is not None:
                                seen.add(mid)
                                if rec.get("save_reason") == "quoted":
                                    quotes.add((mid, _to_int(rec.get("quoted_from"))))
            except OSError:
                continue
        self._seen_ids[store_key] = seen
        self._seen_quotes[store_key] = quotes
        return seen

    # 返回目标群名映射
    def resolve_targets(self, sessions: List[Dict[str, Any]]) -> Dict[str, str]:
        """Map configured group names -> usernames (only group chats)."""
        wanted = set(self.cfg.target_groups or [])
        by_name: Dict[str, str] = {}
        for s in sessions:
            is_group = _to_bool(_first(s, CANDIDATE_KEYS["is_group"]))
            if is_group is False:
                continue
            name = _first(s, CANDIDATE_KEYS["name"])
            username = _first(s, CANDIDATE_KEYS["username"])
            if not username:
                continue
            if name and name in wanted:
                by_name[name] = str(username)
        return by_name

    # ---- 本地模式账号隔离 ----
    def _is_local(self) -> bool:
        """local 数据源：水位/去重/落盘键均带账号前缀，跨账号绝不复用。"""
        return getattr(self.client, "kind", "") == "local"

    def _store_key(self, name: str) -> str:
        """状态字典的键：local 模式加账号前缀（账号切换后互不污染）。"""
        if self._is_local() and self.account:
            return f"{self.account}|{name}"
        return name

    def _record_prefix(self, group_name: str) -> str:
        """JSONL 文件名前缀：local 模式带账号前缀。"""
        safe_group = self._safe_name(group_name)
        if self._is_local() and self.account:
            return f"{self._safe_name(self.account)}_{safe_group}"
        return safe_group

    def _warn_missing_groups(self, missing: Set[str]) -> None:
        """目标群未解析到时告警（逐群限频，避免每轮刷屏）。

        E1 修复背景（2026-09-09 目标机事故）：此前解析失败只在"全部群都
        失败"时打一条 DEBUG，单群失败零痕迹——001记录 全程未被采集却无
        任何日志。现对每个缺失群按 MISSING_GROUP_WARN_INTERVAL 限频打
        WARNING。"""
        now = time.time()
        for name in sorted(missing):
            last = self._missing_warned_at.get(name, 0.0)
            if now - last >= MISSING_GROUP_WARN_INTERVAL:
                self._missing_warned_at[name] = now
                LOG.warning(
                    "目标群未在会话列表解析到，尚未采集（每 %d 秒提醒一次）: %s"
                    " —— 群不活跃或账号不在该群时不会出现在会话列表",
                    int(MISSING_GROUP_WARN_INTERVAL), name)

    # 从拉取会话到轮询拉取消息的一次全流程
    def poll_once(self) -> int:
        # local 数据源：轮询前巡检登录实例（账号切换时重绑并刷新 account）
        pre_poll = getattr(self.client, "pre_poll", None)
        if callable(pre_poll):
            current_account = pre_poll()
            if current_account and current_account != self.account:
                LOG.warning("监控账号已切换: %s -> %s（水位线按账号隔离）",
                            self.account, current_account)
                self.account = current_account

        sessions = self.client.list_sessions(self.account)
        self._resolved = self.resolve_targets(sessions)
        matched = set(self._resolved.values())
        missing = set(self.cfg.target_groups or []) - set(self._resolved.keys())
        if missing:
            self._warn_missing_groups(missing)
        if not matched:
            return 0

        written = 0
        for group_name, username in self._resolved.items():
            written += self._poll_group(group_name, username)
        # Persist after every cycle, even when nothing was written: the group
        # watermark may have advanced past non-target messages, and saving it
        # immediately prevents duplicate records after an unclean restart.
        self._save_state()
        return written

    def _poll_group(self, group_name: str, username: str) -> int:
        """
        采集单个聊天会话的新目标消息，并更新其水位线（watermark）。

        不变式（Invariants）：

        state[username] 存储的是已处理的最旧序列边界（即该序号/时间及以下的所有消息都已被判定过）。
        一条消息被视为“新消息”，当且仅当它比该边界更新，因此 (边界, 最新] 区间内的消息永远不会被跳过。

        翻页采用从新到旧（newest-first） 的方式，并配合递增的偏移量。
        当某一页中包含处于或低于边界的消息时，说明边界以上的所有积压消息都已被看到，此时边界会提升到本轮看到的最新消息位置。

        当达到每周期页数上限时（即积压消息量极大），边界不会提升，而是记住下一个偏移量，以便下一个周期从本次停止的位置精确续传。

        发生崩溃后，只有边界会保留；恢复后遍历会从顶部重新开始，但已写入记录会通过 msg_id 去重跳过。
        """
        state_key = self._store_key(username)
        last = self.state.get(state_key)
        # 未记录的群
        if last is None:
            # Baseline on first contact: remember the watermark (the newest
            # message seen), then optionally backfill the most recent N target
            # messages so recording starts N messages back instead of empty.
            raw_msgs = self.client.list_messages(
                self.account, username, self.cfg.page_limit,
                order="desc", source=self.cfg.source)
            if not raw_msgs:
                return 0
            # 取最新的消息
            seqs = [seq_of(m) for m in raw_msgs]
            max_seq = max((s for s, _ in seqs if s is not None), default=None)
            max_time = max((t for _, t in seqs if t is not None), default=None)
            # 新建水位线
            self.state[state_key] = {"seq": max_seq, "time": max_time}
            written = 0
            if self.cfg.backtrack_count and self.cfg.backtrack_count > 0:
                # 回溯历史消息
                written = self._backfill(group_name, username, raw_msgs)
            # E2 修复：首触基线会吞掉"唤醒会话的那条消息"（群不活跃时首次
            # 解析恰由新消息触发，而基线把当时的最新消息直接标记为已处理，
            # 2026-09-09 目标机实测丢第一条）。此处把首触页内启动后到达的
            # 目标消息补写落盘；启动前的历史积压仍不回采。
            written += self._rescue_first_contact(group_name, username, raw_msgs)
            LOG.info("[%s] baseline watermark set (backfill=%s, %d written)",
                     group_name, self.cfg.backtrack_count, written)
            return written

        # 读取历史水位线
        bound_seq = _to_int(last.get("seq")) if isinstance(last, dict) else _to_int(last)
        bound_time = _to_timestamp(last.get("time")) if isinstance(last, dict) else None

        new_msgs = []            # target messages above the boundary, newest -> oldest
        newest_new = None        # (seq, time) of the newest message above the boundary
        offset = self._page_offsets.get(state_key, 0)
        reached_boundary = False
        pages = 0
        while pages < MAX_INCREMENT_PAGES and not reached_boundary:
            # desc--最新的在前
            raw_msgs = self.client.list_messages(
                self.account, username, self.cfg.page_limit,
                offset=offset, order="desc", source=self.cfg.source)
            if not raw_msgs:
                reached_boundary = True  # nothing more to fetch
                break
            pages += 1
            for m in raw_msgs:
                s, t = seq_of(m)
                if is_newer(s, t, bound_seq, bound_time):
                    if newest_new is None:
                        newest_new = (s, t)  # pages arrive newest-first
                    if is_target_message(m, self.cfg.prefix):
                        new_msgs.append(m)
                else:
                    reached_boundary = True  # this page already contains decided data
                    break
            offset += len(raw_msgs)
            if len(raw_msgs) < self.cfg.page_limit:
                reached_boundary = True  # end of stored history
        self._page_offsets[state_key] = 0 if reached_boundary else offset

        if newest_new is None:
            return 0  # nothing above the boundary

        written = 0
        for m in reversed(new_msgs):  # chronological order
            written += self._write(group_name, username, m)
        if reached_boundary:
            # The whole span above the boundary was scanned this cycle (or is
            # already on disk), so it is safe to raise the boundary.
            self.state[state_key] = {"seq": newest_new[0], "time": newest_new[1]}
        # On a truncated cycle the boundary is kept; pages resumed next cycle.
        return written

    def _backfill(self, group_name: str, username: str,
                  first_page: List[Dict[str, Any]]) -> int:
        """On first contact, record the most recent ``backtrack_count`` target
        messages going back from the live frontier.

        ``first_page`` (newest-first) is reused; if the requested window is
        larger than one page we page backwards (increasing offset) until we have
        enough messages or hit the end of history. A hard safety cap of
        ``page_limit * MAX_INCREMENT_PAGES`` prevents a runaway backfill when an
        absurd count is entered. Only target messages (``is_target_message``)
        are written, and each carries its quoted history exactly like live
        messages. The watermark was already set to the frontier, so the next
        poll cycle only records newer messages and never duplicates this window.
        """
        n = self.cfg.backtrack_count
        if not n or n <= 0:
            return 0
        hard_cap = self.cfg.page_limit * MAX_INCREMENT_PAGES
        collected: List[Dict[str, Any]] = list(first_page)  # newest-first
        while len(collected) < n and len(collected) < hard_cap:
            offset = len(collected)
            page = self.client.list_messages(
                self.account, username, self.cfg.page_limit,
                offset=offset, order="desc", source=self.cfg.source)
            if not page:
                break
            collected.extend(page)
            if len(page) < self.cfg.page_limit:
                break  # end of stored history
        # Keep the N most recent messages (top of the newest-first list).
        window = collected[:n]
        written = 0
        for m in reversed(window):  # chronological order
            if is_target_message(m, self.cfg.prefix):
                written += self._write(group_name, username, m)
        return written

    def _rescue_first_contact(self, group_name: str, username: str,
                              first_page: List[Dict[str, Any]]) -> int:
        """E2 修复：首触基线后补写"启动后到达"的目标消息。

        群会话按活跃度维护（E1），不活跃群要等新消息才可解析——于是唤醒
        会话的那条目标消息恰逢首触基线，被水位直接跳过（2026-09-09 目标机
        实测 003记录 第一条丢失）。此处在基线生效后，把首触页内 create_time
        晚于 monitor 启动时刻（含 FIRST_CONTACT_RESCUE_GRACE 时钟容差）的
        目标前缀消息补写落盘；启动前的历史积压仍不回采。与 _backfill 窗口
        重叠时由 msg_id 去重兜底，不会重复写。create_time 缺失的消息按
        积压处理（保守跳过）。"""
        cutoff = self._started_at - FIRST_CONTACT_RESCUE_GRACE
        rescued: List[Dict[str, Any]] = []
        for m in first_page:                      # newest-first
            if not is_target_message(m, self.cfg.prefix):
                continue
            t = _to_timestamp(extract_fields(m).get("create_time"))
            if t is None or t < cutoff:
                continue
            rescued.append(m)
        written = 0
        for m in reversed(rescued):               # chronological order
            written += self._write(group_name, username, m)
        return written

    @staticmethod
    def _safe_name(group_name: str) -> str: # 群名转化成合法的文件名
        """Make a group name safe for use in a filename while staying readable."""
        if not group_name:
            return "unknown_group"
        name = re.sub(r'[\\/*?:"<>|\s]+', "_", group_name).strip("_")
        if not name:
            return "unknown_group"
        if name.lower() in RESERVED_FILENAMES:
            name = f"_{name}"
        if len(name) > MAX_FILENAME_PART_LEN:
            name = name[:MAX_FILENAME_PART_LEN].rstrip("_") or "unknown_group"
        return name

    # 获取消息的本地日期
    def _date_str(self, raw: Dict[str, Any]) -> str:
        """Local date (YYYYMMDD) of the message, derived from create_time."""
        ctime = _to_timestamp(extract_fields(raw).get("create_time"))
        if ctime is None:
            return date.today().strftime("%Y%m%d")
        try:
            return datetime.fromtimestamp(ctime).strftime("%Y%m%d")
        except (ValueError, OverflowError, OSError):
            # out-of-range timestamp (bad unit, platform limit): bucket today
            return date.today().strftime("%Y%m%d")

    # 将消息写入文件
    def _append_record(self, group_name: str, username: str, raw_for_date: Dict[str, Any],
                       record: Dict[str, Any]) -> bool:
        """Append one record dict to the per-group daily JSONL file.

        Returns True only when a record was actually written. Records whose
        msg_id was already seen (pre-loaded from existing files or written
        earlier in this run) are skipped, so a replay after a crash is
        idempotent. A single failing write is logged and skipped instead of
        letting the error escape and kill the monitor loop.
        """
        msg_id = record.get("msg_id")
        is_quoted = record.get("save_reason") == "quoted"
        store_key = self._store_key(group_name)
        pair: Optional[Tuple[Optional[int], Optional[int]]] = None
        if msg_id is not None:
            # 惰性加载去重状态（原生消息 id 集合 + 引用记录键对集合）
            if self._seen_ids.get(store_key) is None:
                self._load_seen_ids(group_name)
            if is_quoted:
                # 引用记录按 (quoted_id, quoted_from) 复合键去重：同一条历史
                # 消息被不同消息引用是独立事件，各需落盘一次。旧版按 msg_id
                # 单键去重会导致第二次引用被静默丢弃（B,A,C 缺失第二条 A）。
                pair = (msg_id, _to_int(record.get("quoted_from")))
                if pair in self._seen_quotes[store_key]:
                    return False
            elif msg_id in self._seen_ids[store_key]:
                return False
        try:
            path = self.output_dir / f"{self._record_prefix(group_name)}_{self._date_str(raw_for_date)}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                # fsync：断电时尾部撕裂的行 = 该消息永久丢失（水位已推进
                # 不会重采）；记录频率低，fsync 代价可忽略
                os.fsync(fh.fileno())
        except OSError as exc:
            LOG.exception("failed writing record for group %s (msg_id=%s): %s",
                          group_name, msg_id, exc)
            return False
        if msg_id is not None:
            if is_quoted:
                self._seen_quotes[store_key].add(pair)
            else:
                self._seen_ids[store_key].add(msg_id)
        LOG.info("[%s] %s: %s -> %s", group_name, record.get("sender"), record.get("content"), path.name)
        return True

    def _build_quoted_record(self, group_name: str, username: str, raw: Dict[str, Any],
                             main_f: Dict[str, Any], quote: Dict[str, Any],
                             main_msg_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """Build the record for a message referenced by a '#' quote (rule 2+3)."""
        quoted_id = _to_int(quote.get("quote_server_id"))
        quoted_content = quote.get("quote_content") or ""
        if quoted_id is None:
            # The original message may have been recalled: the backend returns no
            # usable id but often keeps the preview text. Never drop the content
            # (rule 2) -- derive a stable id so the record is still written and
            # stays idempotent across restarts/replays.
            if not quoted_content:
                return None
            stable = int(hashlib.md5(
                f"{quote.get('quote_username')}|{quoted_content}".encode("utf-8")
            ).hexdigest()[:15], 16)
            quoted_id = -(stable % (10 ** 18))   # negative to avoid clashing with real ids
            if quoted_id == 0:
                quoted_id = -1
            LOG.warning("quoted message id unavailable (original likely recalled); "
                        "derived stable id %s to preserve content", quoted_id)
        marker = self.cfg.quote_marker
        return {
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "group": group_name,
            "group_username": username,
            "sender": quote.get("quote_username"),
            "content": f"{marker}{quoted_content}",
            "msg_id": quoted_id,
            # The quote payload carries no own timestamp; borrow the quoting
            # message's time so the record files under the same day.
            "create_time": _to_timestamp(main_f.get("create_time")),
            "is_self": None,
            "prefix": marker,
            "save_reason": "quoted",
            "quoted_from": main_msg_id,
            "quote_type": quote.get("quote_type"),
        }

    #
    def _write(self, group_name: str, username: str, raw: Dict[str, Any]) -> int:
        """Persist a target message and, when it quotes history, the referenced
        one too. Returns the number of records actually written (0, 1 or 2).

        * Rule 1: the '#' message is always saved (prefix kept as configured).
        * Rule 2+3: if it references a historical message, that message is also
          saved, flagged with ``quote_marker`` and linked via ``quoted_from``.
        """
        f = extract_fields(raw)
        main_msg_id = _to_int(f.get("msg_id"))
        main_record = {
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "group": group_name,
            "group_username": username,
            "sender": f["sender"],
            "content": f["content"],
            "msg_id": main_msg_id,
            "create_time": _to_timestamp(f["create_time"]),
            "is_self": f["is_self"],
            "prefix": self.cfg.prefix,
            "save_reason": "direct",
        }

        written = 0
        quote = extract_quote(raw)
        if quote:
            main_record["has_quote"] = True
        if self._append_record(group_name, username, raw, main_record):
            written += 1
        quoted_record = None
        if quote:
            quoted_record = self._build_quoted_record(
                group_name, username, raw, f, quote, main_msg_id)
            if quoted_record is not None and self._append_record(
                    group_name, username, raw, quoted_record):
                written += 1
        # 推送服务 A：一条 // 触发消息与其引用文本拼成一次推送
        if self.pusher and (f["content"] or "").lstrip().startswith(self.cfg.prefix):
            self.pusher.push(
                group=group_name,
                sender=f["sender"],
                msg_id=main_msg_id,
                create_time=_to_timestamp(f["create_time"]),
                content=f["content"],
                quoted_content=(quoted_record or {}).get("content", "")
                if quoted_record else "",
                quoted_msg_id=(quoted_record or {}).get("msg_id")
                if quoted_record else None,
            )
        return written

    # ---- lifecycle ----
    def run(self) -> None:
        LOG.info("monitor started; watching groups: %s",
                 ", ".join(self.cfg.target_groups or []))
        LOG.info("output dir -> %s", self.output_dir.resolve())
        while not self._stop.is_set():
            try:
                n = self.poll_once()
                if n:
                    LOG.info("flushed %d message(s)", n)
            except ApiError as exc:
                LOG.warning("poll error: %s", exc)
            self._stop.wait(self.cfg.poll_interval)
        self._save_state()
        if self.pusher:
            self.pusher.close()
        LOG.info("monitor stopped")

    def stop(self) -> None:
        self._stop.set()


def print_sample(client: ApiClient, cfg: Config, account: str) -> None:
    """Dump one real message object so the user can confirm field names."""
    sessions = client.list_sessions(account)
    groups = [s for s in sessions
              if _to_bool(_first(s, CANDIDATE_KEYS["is_group"]))]
    print(f"# sessions total={len(sessions)} groups={len(groups)}")
    print("# sample session (first group):")
    print(json.dumps(groups[0] if groups else (sessions[0] if sessions else {}),
                     ensure_ascii=False, indent=2))
    if groups:
        uname = _first(groups[0], CANDIDATE_KEYS["username"])
        msgs = client.list_messages(account, str(uname), 3, order="desc")
        print(f"# sample messages for {uname} (count={len(msgs)}):")
        for m in msgs:
            print(json.dumps(m, ensure_ascii=False, indent=2))


def load_config(path: Optional[str]) -> Config:
    cfg = Config()
    cp = configparser.ConfigParser()
    if path and Path(path).exists():
        cp.read(path, encoding="utf-8")
        if cp.has_section("monitor"):
            sec = cp["monitor"]
            cfg.host = sec.get("host", cfg.host)
            cfg.port = int(sec.get("port", str(cfg.port)))
            cfg.prefix = sec.get("prefix", cfg.prefix)
            cfg.poll_interval = float(sec.get("poll_interval", str(cfg.poll_interval)))
            cfg.page_limit = int(sec.get("page_limit", str(cfg.page_limit)))
            cfg.account = sec.get("account", cfg.account)
            cfg.output_dir = sec.get("output_dir", cfg.output_dir)
            cfg.state_file = sec.get("state_file", cfg.state_file)
            cfg.quote_marker = sec.get("quote_marker", cfg.quote_marker)
            cfg.backtrack_count = int(sec.get("backtrack_count", str(cfg.backtrack_count)))
            cfg.start_mode = sec.get("start_mode", cfg.start_mode)
            cfg.service_a_url = sec.get("service_a_url", cfg.service_a_url)
            cfg.service_a_token = sec.get("service_a_token", cfg.service_a_token)
            if cp.has_section("data_source"):
                dsrc = cp["data_source"]
                cfg.data_source = dsrc.get("source", cfg.data_source).strip().lower()
                cfg.db_storage_path = dsrc.get("data_root", cfg.db_storage_path)
                cfg.snapshot_dir = dsrc.get("snapshot_dir", cfg.snapshot_dir)
            groups = sec.get("target_groups", "")
            cfg.target_groups = [g.strip() for g in groups.split(",") if g.strip()]
    # env overrides
    cfg.host = _env("WECHAT_API_HOST", cfg.host)
    cfg.port = int(_env("WECHAT_API_PORT", str(cfg.port)))
    cfg.account = _env("WECHAT_ACCOUNT", cfg.account)
    cfg.prefix = _env("WECHAT_PREFIX", cfg.prefix)
    cfg.output_dir = _env("WECHAT_OUTPUT_DIR", cfg.output_dir)
    cfg.quote_marker = _env("WECHAT_QUOTE_MARKER", cfg.quote_marker)
    cfg.backtrack_count = int(_env("WECHAT_BACKTRACK_COUNT", str(cfg.backtrack_count)))
    cfg.start_mode = _env("WECHAT_START_MODE", cfg.start_mode)
    cfg.service_a_url = _env("WECHAT_SERVICE_A_URL", cfg.service_a_url)
    cfg.service_a_token = _env("WECHAT_SERVICE_A_TOKEN", cfg.service_a_token)
    cfg.data_source = _env("WECHAT_DATA_SOURCE", cfg.data_source).strip().lower()
    cfg.db_storage_path = _env("WECHAT_DB_STORAGE_PATH", cfg.db_storage_path)
    cfg.snapshot_dir = _env("WECHAT_SNAPSHOT_DIR", cfg.snapshot_dir)
    if cfg.data_source not in ("local", "remote", "auto"):
        LOG.warning("非法数据源 %r，回退 auto", cfg.data_source)
        cfg.data_source = "auto"
    if not cfg.target_groups:
        env_groups = _env("WECHAT_TARGET_GROUPS", "")
        cfg.target_groups = [g.strip() for g in env_groups.split(",") if g.strip()]
    if not cfg.target_groups:
        raise SystemExit("no target_groups configured (set target_groups in config.ini "
                         "or WECHAT_TARGET_GROUPS env)")
    return cfg


def _env(name: str, default: str) -> str:
    return __import__("os").environ.get(name, default)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Monitor WeChat group '#' messages.")
    # 短参 -c 与服务 A 的 _parse_args 对齐：ops_ui 以
    # [python, wechat_monitor.py, -c, config.ini] 统一启动两进程——
    # 此前只认 --config，UI 启动即 argparse 退出码 2（2026-09-04 实测）
    p.add_argument("-c", "--config", default="config.ini", help="path to config.ini")
    p.add_argument("--print-sample", action="store_true",
                   help="dump a real session+message sample and exit (use to verify fields)")
    p.add_argument("--once", action="store_true",
                   help="run a single poll cycle then exit")
    return p.parse_args()


# 0-只记启动后新消息, 1-从断点续传
def prompt_start_mode(cfg: Config) -> None:
    """Ask the user, at startup, which watermark mode to run in.

    No command-line flag is used (the program is meant to be packaged as an
    exe); the choice comes from interactive stdin:

    * ``0`` -> ``fresh``: discard the previous watermark and only record
      messages that arrive *after* this start (any backlog accumulated while
      the monitor was offline is intentionally dropped).
    * ``1`` -> ``resume``: continue from the saved ``.monitor_state.json``
      breakpoint and catch up on offline backlog (default on empty input).

    The configured default (``cfg.start_mode``) is kept when input is empty or
    invalid, so a packaged exe with no TTY falls back gracefully.
    """
    default = cfg.start_mode
    try:
        raw = input(
            f"启动模式 (0=只记启动后新消息, 1=从断点续传, 回车=默认 {default}): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print()  # tidy newline after ^C/^D
        return
    if not raw:
        return  # keep configured default
    if raw == "0":
        cfg.start_mode = "fresh"
    elif raw == "1":
        cfg.start_mode = "resume"
    else:
        LOG.warning("invalid start mode %r, keeping default %s", raw, default)


def _force_utf8_stdio() -> None:
    """强制 stdio 为 UTF-8（源码/打包通用，不依赖环境变量）.

    背景：stdout 接管道时 Python 默认按 ANSI 代码页（本机 GBK）写入，
    而 UI 固定按 UTF-8 读取；实测 PyInstaller frozen exe 不理会
    PYTHONIOENCODING 环境变量（2026-09-04），故在入口处直接 reconfigure。
    windowed 模式 sys.stdout 可能为 None，逐流 try。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        except (ValueError, OSError, AttributeError):
            pass


def build_client(cfg: Config):
    """按配置构建数据源客户端：local / remote / auto（优先 local）。"""
    if cfg.data_source == "remote":
        return ApiClient(cfg)
    if cfg.data_source == "local":
        from wechat_data.provider import LocalProvider
        return LocalProvider.try_create(
            explicit_data_root=cfg.db_storage_path,
            explicit_account=cfg.account,
            snapshot_root=Path(cfg.snapshot_dir) if cfg.snapshot_dir else None)
    # auto：优先本地直连；不可用（未装微信/未登录）回退项目 A API
    try:
        from wechat_data.provider import LocalProvider
        from wechat_data.account_guard import AccountGuardError
        candidate = LocalProvider.try_create(
            explicit_data_root=cfg.db_storage_path,
            explicit_account=cfg.account,
            snapshot_root=Path(cfg.snapshot_dir) if cfg.snapshot_dir else None)
        candidate.health()
        LOG.info("数据源 auto -> local（本机微信可用）")
        return candidate
    except Exception as exc:                  # noqa: BLE001 - 回退必须兜底
        LOG.info("数据源 auto -> remote（本机微信不可用: %s）", exc)
        return ApiClient(cfg)


def main() -> None:
    _force_utf8_stdio()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args()
    config_path = args.config
    if getattr(sys, "frozen", False) and not Path(config_path).is_absolute():
        # 打包铁律：双击 exe 时 cwd 不定，默认配置相对 exe 所在目录锚定
        #（运维台 spawn 分支已传绝对路径，此处兜底直接双击形态）
        try:
            from wechat_data.paths import get_base_dir
            config_path = str(get_base_dir() / config_path)
        except ImportError:
            pass
    try:
        cfg = load_config(config_path)
    except SystemExit:
        raise

    client = build_client(cfg)
    if args.print_sample:
        client.wait_health()
        account = _resolve_account(cfg, client)
        print_sample(client, cfg, account)
        return

    try:
        client.wait_health()
    except Exception as exc:                  # noqa: BLE001 - 入口友好报错
        LOG.exception("数据源初始化失败（完整堆栈如下）")
        LOG.error("数据源初始化失败: %s", exc)
        raise SystemExit(1)
    account = _resolve_account(cfg, client)
    if not account:
        LOG.warning("no account specified and auto-discovery failed; "
                    "trying with empty account (some setups accept this)")

    prompt_start_mode(cfg)

    monitor = Monitor(client, cfg, account)

    def _handle(signum, _frame):
        LOG.info("received signal %s, stopping ...", signum)
        monitor.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    # 运维管理台（ops_ui）以 CREATE_NEW_PROCESS_GROUP 启动本进程，该组内
    # CTRL_C_EVENT 被系统禁用，仅 CTRL_BREAK_EVENT（SIGBREAK）可达。
    # 注册同一处理器：UI「结束」时走优雅停止并保存断点（实测 2026-09-04）。
    signal.signal(signal.SIGBREAK, _handle)

    if args.once:
        n = monitor.poll_once()
        monitor._save_state()
        LOG.info("single run done, %d new message(s)", n)
        return
    monitor.run()


if __name__ == "__main__":
    main()
