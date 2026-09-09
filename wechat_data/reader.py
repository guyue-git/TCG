"""解密快照读取器 —— 会话与消息查询，字段对齐 wechat_monitor 归一化。

微信 4.x 库结构在不同小版本间存在漂移，本模块延续 monitor 的
CANDIDATE_KEYS 防御策略：
* 运行时通过 sqlite_master + PRAGMA table_info 发现表与列；
* 会话表/消息表/联系人表均按候选列名逐个命中；
* 群消息发送者经 Name2ID（SenderTalkerId -> wxid）解析；
* 引用消息（type 49 refermsg XML）解析为 quoteServerId/quoteContent 等
  字段，与 monitor 的 extract_quote 无缝衔接；
* CompressContent 为 zstd 压缩，zstandard 缺失时优雅降级为空内容。
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("wechat_data.reader")

try:                                        # 可选依赖：zstd 压缩内容解压
    import zstandard as _zstd
except ImportError:                          # pragma: no cover - 环境差异
    _zstd = None

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

_SESSION_DB_NAMES = {"session.db", "sessiondata.db", "micromsg.db"}
_CONTACT_DB_NAMES = {"contact.db", "micromsg.db"}
_MESSAGE_DB_RE = re.compile(r"^(message(?:_\d+)?|msg\d*|micromsg)\.db$",
                            re.IGNORECASE)

_COL_CANDIDATES = {
    "id": ["localid", "local_id", "msglocalid"],
    "svrid": ["msgsvrid", "msgsvrid".lower(), "server_id", "serverid"],
    "content": ["strcontent", "message_content", "content"],
    "compress": ["compresscontent", "compress_content"],
    "time": ["createtime", "create_time", "ntime", "timestamp"],
    "is_send": ["issender", "is_sender", "issend", "issent"],
    "type": ["type", "message_type", "msgtype"],
    "sender_talker_id": ["sendertalkerid", "sender_talker_id"],
    "talker": ["strtalker", "talker", "strtalkerid"],
}
_SESSION_USERNAME_COLS = ["strusrname", "usrname", "username", "username".lower(),
                          "strusename", "talker"]
_SESSION_NAME_COLS = ["nickname", "nick_name", "remark", "displayname", "name"]
_SESSION_TIME_COLS = ["lasttimestamp", "last_timestamp", "ntime", "createtime",
                      "createtime".lower()]
_CONTACT_USERNAME_COLS = ["username", "usrname", "username"]
_CONTACT_NAME_COLS = ["nickname", "nick_name", "remark"]
_ID2NAME_ID_COLS = ["id", "usrname_id", "nameid"]
_ID2NAME_NAME_COLS = ["usrname", "username", "user_name", "name"]


def _norm(name: str) -> str:
    return str(name or "").strip().lower()


def _pick_column(columns: List[str], candidates: List[str]) -> Optional[str]:
    """按候选名（小写比较）返回实际存在的列名（保留原始大小写）。"""
    lowered = {_norm(c): c for c in columns}
    for candidate in candidates:
        actual = lowered.get(_norm(candidate))
        if actual:
            return actual
    return None


class MessageSnapshotReader:
    """读取一份解密快照目录（<snapshot_root>/<account>/）。"""

    def __init__(self, snapshot_dir: str | Path):
        self.snapshot_dir = Path(snapshot_dir)
        self._table_columns: Dict[Tuple[str, str], List[str]] = {}

    # ---- 底层工具 ----

    def _databases(self, name_filter) -> List[Path]:
        if not self.snapshot_dir.is_dir():
            return []
        return sorted(
            (p for p in self.snapshot_dir.rglob("*.db")
             if name_filter(p.name.lower())),
            key=lambda p: str(p).lower())

    def _connect(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _table_names(self, conn: sqlite3.Connection) -> List[str]:
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return [str(r[0]) for r in rows]
        except sqlite3.Error as exc:
            LOG.warning("表清单读取失败 %s: %s", conn, exc)
            return []

    def _columns_of(self, db_path: Path, table: str) -> List[str]:
        key = (str(db_path), table)
        cached = self._table_columns.get(key)
        if cached is not None:
            return cached
        try:
            conn = self._connect(db_path)
            try:
                rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                columns = [str(r[1]) for r in rows]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            LOG.warning("表结构读取失败 %s.%s: %s", db_path.name, table, exc)
            columns = []
        self._table_columns[key] = columns
        return columns

    # ---- 会话列表 ----

    def list_sessions(self, limit: int = 400) -> List[Dict[str, Any]]:
        """返回会话列表，字段：username / name / isGroup / create_time。

        E1 修复（2026-09-09 目标机事故）：微信 4.x 会话表按活跃度维护且本
        函数对会话表有 LIMIT 截断，不活跃的目标群可能不在会话库可见集合里
        （实测 001记录 全程未被采集）。故在会话库结果之外，从 contact.db
        通讯录兜底补全全部群聊会话；会话库已有条目优先（昵称/时间更准），
        兜底条目不占用 limit 名额，因此返回条数可能略超 limit。
        """
        sessions: Dict[str, Dict[str, Any]] = {}
        contact_names = self._contact_name_map()
        for db_path in self._databases(lambda n: n in _SESSION_DB_NAMES):
            try:
                conn = self._connect(db_path)
            except sqlite3.Error as exc:
                LOG.warning("会话库打开失败 %s: %s", db_path, exc)
                continue
            try:
                for table in self._table_names(conn):
                    columns = self._columns_of(db_path, table)
                    username_col = _pick_column(columns, _SESSION_USERNAME_COLS)
                    if not username_col:
                        continue
                    name_col = _pick_column(columns, _SESSION_NAME_COLS)
                    time_col = _pick_column(columns, _SESSION_TIME_COLS)
                    try:
                        rows = conn.execute(
                            f'SELECT * FROM "{table}" LIMIT ?', (limit,)).fetchall()
                    except sqlite3.Error as exc:
                        LOG.warning("会话表读取失败 %s.%s: %s",
                                    db_path.name, table, exc)
                        continue
                    for row in rows:
                        username = str(row[username_col] or "").strip()
                        if not username or username in sessions:
                            continue
                        name = str(row[name_col] or "").strip() if name_col else ""
                        if not name:
                            name = contact_names.get(username, username)
                        ctime = None
                        if time_col:
                            try:
                                ctime = int(row[time_col])
                            except (TypeError, ValueError):
                                ctime = None
                        sessions[username] = {
                            "username": username,
                            "name": name or username,
                            "isGroup": username.endswith("@chatroom"),
                            "create_time": ctime,
                        }
            finally:
                conn.close()
        # contact 兜底：会话库没有的群聊从通讯录补全（E1 修复）
        contact_only: List[Dict[str, Any]] = []
        for username, name in self._contact_group_map().items():
            if username not in sessions:
                contact_only.append({
                    "username": username,
                    "name": name or username,
                    "isGroup": True,
                    "create_time": None,
                })
        return list(sessions.values())[:limit] + contact_only

    def _contact_group_map(self) -> Dict[str, str]:
        """通讯录中的群聊映射（username -> 群名），供会话兜底解析（E1）。

        只收 @chatroom 群聊；群名优先 NickName，其次 Remark。"""
        mapping: Dict[str, str] = {}
        for db_path in self._databases(lambda n: n in _CONTACT_DB_NAMES):
            try:
                conn = self._connect(db_path)
            except sqlite3.Error:
                continue
            try:
                for table in self._table_names(conn):
                    if _norm(table) not in {"contact", "contacttable"}:
                        continue
                    columns = self._columns_of(db_path, table)
                    username_col = _pick_column(columns, _CONTACT_USERNAME_COLS)
                    if not username_col:
                        continue
                    nick_col = _pick_column(columns, ["nickname", "nick_name"])
                    remark_col = _pick_column(columns, ["remark", "displayname"])
                    select_cols = [username_col]
                    if nick_col:
                        select_cols.append(nick_col)
                    if remark_col:
                        select_cols.append(remark_col)
                    try:
                        cols_sql = ", ".join(f'"{c}"' for c in select_cols)
                        rows = conn.execute(
                            f'SELECT {cols_sql} FROM "{table}"').fetchall()
                    except sqlite3.Error:
                        continue
                    for row in rows:
                        username = str(row[0] or "").strip()
                        if not username.endswith("@chatroom"):
                            continue
                        nick = str(row[1] or "").strip() if nick_col else ""
                        remark = (str(row[2] or "").strip()
                                  if remark_col and len(row) > 2 else "")
                        mapping[username] = nick or remark
            finally:
                conn.close()
        return mapping

    def _contact_name_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for db_path in self._databases(lambda n: n in _CONTACT_DB_NAMES):
            try:
                conn = self._connect(db_path)
            except sqlite3.Error:
                continue
            try:
                for table in self._table_names(conn):
                    if _norm(table) not in {"contact", "contacttable"}:
                        continue
                    columns = self._columns_of(db_path, table)
                    username_col = _pick_column(columns, _CONTACT_USERNAME_COLS)
                    name_col = _pick_column(columns, _CONTACT_NAME_COLS)
                    if not username_col or not name_col:
                        continue
                    try:
                        rows = conn.execute(
                            f'SELECT "{username_col}", "{name_col}" '
                            f'FROM "{table}"').fetchall()
                    except sqlite3.Error:
                        continue
                    for row in rows:
                        username = str(row[0] or "").strip()
                        if username:
                            mapping[username] = str(row[1] or "").strip()
            finally:
                conn.close()
        return mapping

    # ---- 消息列表 ----

    def list_messages(self, username: str, limit: int = 50,
                      offset: int = 0, order: str = "desc") -> List[Dict[str, Any]]:
        """返回某会话的消息（newest-first 分页），字段对齐 CANDIDATE_KEYS。"""
        messages: List[Dict[str, Any]] = []
        for db_path in self._databases(
                lambda n: bool(_MESSAGE_DB_RE.fullmatch(n))):
            page = self._messages_from_db(db_path, username, limit, offset, order)
            messages.extend(page)
        # 多库时按 id 降序重排（本地 id 跨库不重叠，快照内一致）
        messages.sort(key=lambda m: m.get("localId") or 0, reverse=order == "desc")
        return messages[:limit]

    def _messages_from_db(self, db_path: Path, username: str, limit: int,
                          offset: int, order: str) -> List[Dict[str, Any]]:
        try:
            conn = self._connect(db_path)
        except sqlite3.Error as exc:
            LOG.warning("消息库打开失败 %s: %s", db_path, exc)
            return []
        try:
            id2name = self._id2name_map(conn, db_path)
            for table in self._table_names(conn):
                columns = self._columns_of(db_path, table)
                if not self._match_message_table(
                        conn, db_path, table, columns, username):
                    continue
                results = self._rows_to_messages(
                    conn, db_path, table, columns, username, id2name,
                    limit, offset, order)
                if results:
                    return results
            return []
        finally:
            conn.close()

    def _id2name_map(self, conn: sqlite3.Connection,
                     db_path: Path) -> Dict[int, str]:
        mapping: Dict[int, str] = {}
        for table in self._table_names(conn):
            if "name2id" not in _norm(table) and "id2name" not in _norm(table):
                continue
            columns = self._columns_of(db_path, table)
            name_col = _pick_column(columns, _ID2NAME_NAME_COLS)
            id_col = _pick_column(columns, _ID2NAME_ID_COLS)
            if not name_col or not id_col:
                continue
            try:
                rows = conn.execute(
                    f'SELECT "{id_col}", "{name_col}" FROM "{table}"').fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                try:
                    mapping[int(row[0])] = str(row[1] or "").strip()
                except (TypeError, ValueError):
                    continue
        return mapping

    def _match_message_table(
            self, conn: sqlite3.Connection, db_path: Path, table: str,
            columns: List[str], username: str) -> bool:
        """判断该表是否承载 username 的消息（优先按 md5 命名命中）。"""
        lowered = _norm(table)
        md5_suffix = hashlib.md5(username.encode("utf-8")).hexdigest()
        if lowered.endswith(md5_suffix):
            return True
        talker_col = _pick_column(columns, _COL_CANDIDATES["talker"])
        if talker_col:
            return True                      # 单表多会话布局，按 talker 过滤
        return False

    def _rows_to_messages(
            self, conn: sqlite3.Connection, db_path: Path, table: str,
            columns: List[str], username: str,
            id2name: Dict[int, str], limit: int, offset: int,
            order: str) -> List[Dict[str, Any]]:
        id_col = _pick_column(columns, _COL_CANDIDATES["id"])
        content_col = _pick_column(columns, _COL_CANDIDATES["content"])
        compress_col = _pick_column(columns, _COL_CANDIDATES["compress"])
        time_col = _pick_column(columns, _COL_CANDIDATES["time"])
        is_send_col = _pick_column(columns, _COL_CANDIDATES["is_send"])
        type_col = _pick_column(columns, _COL_CANDIDATES["type"])
        sender_id_col = _pick_column(columns, _COL_CANDIDATES["sender_talker_id"])
        talker_col = _pick_column(columns, _COL_CANDIDATES["talker"])
        if not id_col or not (content_col or compress_col):
            return []

        md5_suffix = hashlib.md5(username.encode("utf-8")).hexdigest()
        per_chat = _norm(table).endswith(md5_suffix)
        direction = "DESC" if order == "desc" else "ASC"
        sql = f'SELECT * FROM "{table}"'
        params: List[Any] = []
        if not per_chat and talker_col:
            sql += f' WHERE "{talker_col}" = ?'
            params.append(username)
        sql += f' ORDER BY "{id_col}" {direction} LIMIT ? OFFSET ?'
        params.extend([limit, offset])

        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            LOG.warning("消息查询失败 %s.%s: %s", db_path.name, table, exc)
            return []

        contact_names = self._contact_name_map()
        is_group = username.endswith("@chatroom")
        messages: List[Dict[str, Any]] = []
        for row in rows:
            content = self._text_or_decompress(row[content_col]) \
                if content_col else ""
            if not content and compress_col is not None:
                content = self._decompress(row[compress_col])
            sender = ""
            if not per_chat and talker_col:
                sender = str(row[talker_col] or "").strip()
            elif sender_id_col is not None:
                try:
                    sender = id2name.get(int(row[sender_id_col]), "")
                except (TypeError, ValueError):
                    sender = ""
            msg_type = None
            if type_col is not None:
                try:
                    msg_type = int(row[type_col])
                except (TypeError, ValueError):
                    msg_type = None
            quote_fields: Dict[str, Any] = {}
            if msg_type == 49 or "<refermsg" in content:
                quote_fields = self._parse_quote_fields(content)
                # 与项目 A 后端行为对齐：引用消息的正文 = appmsg <title>
                #（用户输入的新文本），保证 monitor 的前缀匹配可用
                if quote_fields.get("_title"):
                    content = quote_fields.pop("_title")
                elif quote_fields:
                    content = ""
            raw: Dict[str, Any] = {
                "localId": row[id_col],
                "message_content": content,
                "senderUsername": sender,
                "senderDisplayName": contact_names.get(sender, sender) or sender,
                "create_time": (row[time_col] if time_col else None),
                "isSent": (bool(row[is_send_col]) if is_send_col else None),
                "isGroup": is_group,
                "message_type": msg_type,
            }
            if quote_fields:
                raw.update(quote_fields)
            messages.append(raw)
        return messages

    @classmethod
    def _text_or_decompress(cls, value: Any) -> str:
        """主内容列取文本；微信 4.x 会把部分消息以 zstd 压缩字节直接存入
        message_content（实测 2026-09-09，如文本「//88」整条压缩），按
        magic 识别后解压，否则按普通文本返回（D3 修复）。"""
        if value is None:
            return ""
        if isinstance(value, (bytes, bytearray, memoryview)):
            return cls._decompress(bytes(value))
        return str(value)

    @staticmethod
    def _decompress(blob: Any) -> str:
        """zstd 解压 CompressContent；不可解时返回空串（优雅降级）。"""
        if not blob:
            return ""
        data = bytes(blob)
        if not data.startswith(_ZSTD_MAGIC):
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return ""
        if _zstd is None:
            LOG.warning("消息内容为 zstd 压缩但未安装 zstandard，"
                        "该条内容降级为空（pip install zstandard 可启用）")
            return ""
        try:
            return _zstd.ZstdDecompressor().decompress(data).decode(
                "utf-8", errors="ignore")
        except Exception as exc:
            LOG.warning("zstd 解压失败: %s", exc)
            return ""

    @staticmethod
    def _parse_quote_fields(content: str) -> Dict[str, Any]:
        """解析 type 49 引用消息 XML -> quoteServerId / quoteContent / ..."""
        fields: Dict[str, Any] = {}
        try:
            xml_start = content.find("<msg")
            if xml_start < 0:
                return fields
            root = ET.fromstring(content[xml_start:])
            title = (root.findtext(".//title") or "").strip()
            if title:
                fields["_title"] = _strip_tags(title)
            refer = root.find(".//refermsg")    # refermsg 嵌套在 appmsg 内
            if refer is None:
                return fields
            svrid = (refer.findtext("svrid") or "").strip()
            if svrid.isdigit():
                fields["quoteServerId"] = int(svrid)
            display = (refer.findtext("displayname") or "").strip()
            chatusr = (refer.findtext("chatusr") or "").strip()
            fields["quoteUsername"] = display or chatusr
            fields["quoteType"] = refer.findtext("type")
            quote_content = refer.findtext("content") or ""
            fields["quoteContent"] = _strip_tags(quote_content)
        except ET.ParseError:
            return fields
        return fields


def _strip_tags(text: str) -> str:
    """去除引用文本中的简单 HTML 标签并反转义实体。"""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()
