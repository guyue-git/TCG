"""服务 A 配置：service_a_config.ini + 默认值."""

from __future__ import annotations

import configparser
import logging
from dataclasses import dataclass, field
from pathlib import Path

from tc_generator.config import DEFAULT_EDGE_PATH, DEFAULT_GROUP_MAP

LOG = logging.getLogger(__name__)


@dataclass
class ServiceConfig:
    host: str = "127.0.0.1"
    port: int = 16320
    inbox_dir: Path = field(default_factory=lambda: Path("service_a_inbox"))
    output_dirname: str = "output"
    workers: int = 4
    counterparty_name: str = "COUNTERPARTY"
    # 渲染内核路径（键名沿用 edge_path；留空 = 自动发现，见
    # tc_generator/edge_locator.py：ini 显式 > env > 随包 headless-shell > Edge）
    edge_path: str = DEFAULT_EDGE_PATH
    group_map: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_GROUP_MAP))
    # monitor 推送令牌：非空时要求请求头 X-Auth-Token 匹配
    auth_token: str = ""

    def resolve(self, base_dir: Path) -> "ServiceConfig":
        self.inbox_dir = (base_dir / self.inbox_dir).resolve()
        return self


def load_service_config(path: str | Path | None,
                        base_dir: Path | None = None) -> ServiceConfig:
    """读取 service_a_config.ini；文件/节缺失时用默认值。"""
    cfg = ServiceConfig()
    if path is not None:
        # configparser.read 对不存在的文件静默忽略——路径拼错会退化成
        # 全默认配置（token 空、群映射缺失），必须显式暴露
        if not Path(path).exists():
            LOG.warning("配置文件不存在，使用全默认值: %s", path)
        cp = configparser.ConfigParser()
        cp.read(str(path), encoding="utf-8")
        if cp.has_section("service"):
            sec = cp["service"]
            cfg.host = sec.get("host", cfg.host)
            cfg.port = int(sec.get("port", str(cfg.port)))
            cfg.inbox_dir = Path(sec.get("inbox_dir", str(cfg.inbox_dir)))
            cfg.output_dirname = sec.get("output_dirname", cfg.output_dirname)
            cfg.workers = int(sec.get("workers", str(cfg.workers)))
            cfg.counterparty_name = sec.get(
                "counterparty_name", cfg.counterparty_name)
            cfg.edge_path = sec.get("edge_path", cfg.edge_path)
            cfg.auth_token = sec.get("auth_token", cfg.auth_token)
        if cp.has_section("groups"):
            cfg.group_map.update(
                {k.strip(): v.strip() for k, v in cp["groups"].items() if v.strip()})
    if base_dir is not None:
        cfg.resolve(base_dir)
    return cfg
