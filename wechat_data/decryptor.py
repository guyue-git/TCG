"""SQLCipher 4 逐页解密器。

移植自 WeChatDataAnalysis WeChatDatabaseDecryptor，保留核心行为：
* page-1 HMAC 先行校验（raw_enc_key / sqlcipher_passphrase 双模式自适应）；
* 非首页 HMAC 异常不丢页（部分微信 4.x 大库在 1GiB 边界会出现单页
  HMAC 不匹配，丢页会导致后续页号整体错位）——记 warning 继续解密；
* 解密产物做 SQLite quick_check，失败即删除输出；
* 读取前后快照源文件 size/mtime，检测解密期间源库变化。
"""

from __future__ import annotations

import hmac
import logging
import shutil
from pathlib import Path
from typing import Any, Dict

from .sqlcipher_spec import (HMAC_SIZE, PAGE_SIZE, RESERVE_SIZE, SALT_SIZE,
                             SQLITE_HEADER, compute_page_hmac,
                             resolve_page1_key_material, decrypt_page,
                             sqlite_quick_check)

LOG = logging.getLogger("wechat_data.decryptor")


class DecryptError(RuntimeError):
    """解密失败（附中文可读原因）。"""


def _file_stat(path: Path) -> Dict[str, Any]:
    try:
        st = path.stat()
        return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


class WeChatDatabaseDecryptor:
    """微信 4.x 数据库解密器（64 位 hex 密钥）。"""

    # P4 降噪：已告警过的（库路径, 页号）集合，跨实例共享（快照周期刷新
    # 每轮新建解密器实例），同一页只打一次 HMAC WARNING。
    _hmac_warned: set = set()

    def __init__(self, key_hex: str):
        key_hex = str(key_hex or "").strip()
        if len(key_hex) != 64:
            raise DecryptError("密钥必须是64位十六进制字符串")
        try:
            self.key_bytes = bytes.fromhex(key_hex)
        except ValueError as exc:
            raise DecryptError("密钥必须是有效的十六进制字符串") from exc

    def decrypt_database(self, db_path: str | Path, output_path: str | Path) -> Dict[str, Any]:
        """解密单个库文件，返回结果摘要（success / failed_pages / ...）。"""
        source = Path(db_path)
        output = Path(output_path)
        result: Dict[str, Any] = {
            "db_path": str(source),
            "db_name": source.name,
            "output_path": str(output),
            "success": False,
            "copied_as_sqlite": False,
            "input_size": 0,
            "output_size": 0,
            "total_pages": 0,
            "successful_pages": 0,
            "failed_pages": 0,
            "hmac_warning_pages": 0,
            "key_mode": "",
            "source_changed_during_read": False,
            "diagnostic_status": "not_run",
            "error": "",
        }

        try:
            stat_before = _file_stat(source)
            with source.open("rb") as fh:
                encrypted_data = fh.read()
            stat_after = _file_stat(source)
            result["source_changed_during_read"] = (
                stat_before.get("size") != stat_after.get("size")
                or stat_before.get("mtime_ns") != stat_after.get("mtime_ns"))
            if result["source_changed_during_read"]:
                LOG.warning("解密读取期间源库发生变化，结果可能不完整: %s", source)
        except OSError as exc:
            result["error"] = f"源库读取失败: {exc}"
            LOG.error(result["error"])
            return result

        result["input_size"] = len(encrypted_data)
        if len(encrypted_data) < PAGE_SIZE:
            result["error"] = "file_too_small"
            return result

        # 已是明文 SQLite：直接复制
        if encrypted_data.startswith(SQLITE_HEADER):
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
            result["copied_as_sqlite"] = True
            result["success"] = True
            result["diagnostic_status"] = "ok"
            result["output_size"] = output.stat().st_size
            return result

        page1 = encrypted_data[:PAGE_SIZE]
        resolved = resolve_page1_key_material(self.key_bytes, page1)
        if resolved is None:
            result["total_pages"] = len(encrypted_data) // PAGE_SIZE
            result["failed_pages"] = 1
            result["error"] = "key_mismatch"
            LOG.warning("page-1 HMAC 校验失败，密钥与该库不匹配: %s", source)
            return result
        enc_key, mac_key, key_mode = resolved
        result["key_mode"] = key_mode

        total_pages = (len(encrypted_data) + PAGE_SIZE - 1) // PAGE_SIZE
        result["total_pages"] = total_pages
        decrypted = bytearray()
        failed_pages = 0
        hmac_warning_pages = 0
        for cur_page in range(total_pages):
            page_num = cur_page + 1
            page = encrypted_data[cur_page * PAGE_SIZE:(cur_page + 1) * PAGE_SIZE]
            if not page:
                break
            if len(page) < PAGE_SIZE:
                page = page + b"\x00" * (PAGE_SIZE - len(page))

            stored_hmac = page[PAGE_SIZE - HMAC_SIZE:PAGE_SIZE]
            expected_hmac = compute_page_hmac(mac_key, page, page_num)
            if not hmac.compare_digest(stored_hmac, expected_hmac):
                # 非首页 HMAC 异常不丢页（1GiB 边界单页异常容错）
                hmac_warning_pages += 1
                # P4 降噪（2026-09-09）：快照周期刷新会反复解密同一库，WAL
                # 活跃页的滑动窗口每轮都触发同一批页告警，刷屏淹没有效日志。
                # 按（库路径, 页号）跨实例去重，同一页只在首次告警。
                warn_key = (str(source), page_num)
                if warn_key not in WeChatDatabaseDecryptor._hmac_warned:
                    WeChatDatabaseDecryptor._hmac_warned.add(warn_key)
                    LOG.warning("page %s HMAC 校验失败，仍继续解密: %s",
                                page_num, source.name)
            try:
                decrypted.extend(decrypt_page(enc_key, page, page_num))
            except Exception as exc:  # AES 失败：记录并跳过该页
                failed_pages += 1
                if failed_pages <= 8:
                    LOG.error("page %s AES 解密失败: %s (%s)",
                              page_num, source.name, exc)

        result["successful_pages"] = total_pages - failed_pages
        result["failed_pages"] = failed_pages
        result["hmac_warning_pages"] = hmac_warning_pages

        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output.with_name(output.name + ".tmp")
        with tmp_path.open("wb") as fh:
            fh.write(decrypted)
            fh.flush()
        tmp_path.replace(output)
        result["output_size"] = output.stat().st_size

        ok, detail = sqlite_quick_check(output)
        result["diagnostic_status"] = "ok" if ok else f"bad: {detail[:120]}"
        if not ok:
            try:
                output.unlink()
            except OSError:
                pass
            result["error"] = f"解密输出完整性校验未通过: {detail[:160]}"
            LOG.error("%s (%s)", result["error"], source)
            return result

        result["success"] = True
        return result


def decrypt_summary(results: list[Dict[str, Any]]) -> str:
    """汇总多个库的解密结果为一句中文消息。"""
    total = len(results)
    ok = sum(1 for r in results if r.get("success"))
    if total <= 0:
        return "未找到可解密的数据库"
    if ok <= 0:
        return "解密失败：数据库校验未通过，密钥可能不匹配当前账号"
    if ok < total:
        return f"解密部分成功：成功 {ok}/{total}"
    return f"解密完成: 成功 {ok}/{total}"
