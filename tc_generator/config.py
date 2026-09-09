"""配置加载：tc_config.ini + 默认值."""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path

# 渲染内核路径（历史键名 edge_path，语义 2026-09-08 扩展为内核可执行文件）。
# 留空 = 自动发现，顺序：ini 显式 > 环境变量 EDGE_PATH > 打包目录
# chrome-headless-shell（仅 frozen 态）> 系统 Edge（注册表/常见目录/PATH），
# 见 edge_locator.py；换机/打包分发不再依赖写死的本机路径
DEFAULT_EDGE_PATH = ""
DEFAULT_COUNTERPARTY_NAME = "COUNTERPARTY"

# 一期默认群名 -> 对手方编号映射（裁决 #6；真英雄为本机联调用群）
DEFAULT_GROUP_MAP: dict[str, str] = {
    "测试001": "CP001",
    "测试003": "CP003",
}


@dataclass
class GeneratorConfig:
    """tc_generator 运行配置。"""

    jsonl_dir: Path = field(default_factory=lambda: Path("wechat_logs"))
    output_dir: Path = field(default_factory=lambda: Path("output"))
    scan_interval: float = 2.0
    state_file: Path = field(default_factory=lambda: Path(".tc_state.json"))
    edge_path: str = DEFAULT_EDGE_PATH
    counterparty_name: str = DEFAULT_COUNTERPARTY_NAME
    group_map: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_GROUP_MAP))

    def resolve_paths(self, base_dir: Path) -> "GeneratorConfig":
        """以 base_dir 为锚点将相对路径转为绝对路径。"""
        self.jsonl_dir = (base_dir / self.jsonl_dir).resolve()
        self.output_dir = (base_dir / self.output_dir).resolve()
        self.state_file = (base_dir / self.state_file).resolve()
        return self


def load_generator_config(path: str | Path | None,
                          base_dir: Path | None = None) -> GeneratorConfig:
    """读取 tc_config.ini；文件或节缺失时用默认值。"""
    cfg = GeneratorConfig()
    if path is not None:
        cp = configparser.ConfigParser()
        cp.read(str(path), encoding="utf-8")
        if cp.has_section("generator"):
            sec = cp["generator"]
            cfg.jsonl_dir = Path(sec.get("jsonl_dir", str(cfg.jsonl_dir)))
            cfg.output_dir = Path(sec.get("output_dir", str(cfg.output_dir)))
            cfg.scan_interval = float(
                sec.get("scan_interval", str(cfg.scan_interval)))
            cfg.state_file = Path(sec.get("state_file", str(cfg.state_file)))
            cfg.edge_path = sec.get("edge_path", cfg.edge_path)
            cfg.counterparty_name = sec.get(
                "counterparty_name", cfg.counterparty_name)
        if cp.has_section("groups"):
            cfg.group_map.update(
                {k.strip(): v.strip() for k, v in cp["groups"].items() if v.strip()})
    if base_dir is not None:
        cfg.resolve_paths(base_dir)
    return cfg
