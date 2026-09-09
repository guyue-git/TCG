"""wechat_monitor 命令行参数回归测试。

背景（2026-09-04 实测）：ops_ui 以 [python, wechat_monitor.py, -c, config.ini]
统一启动两进程，但 monitor 的 argparse 只认 --config，短参 -c 未注册 →
argparse 报 unrecognized arguments 并以退出码 2 结束，UI 显示「异常退出
(code=2)」。修复：-c 与服务 A 的 _parse_args 对齐。
"""

from __future__ import annotations

import sys

import pytest

from wechat_monitor import parse_args


def test_short_config_flag_accepted(monkeypatch):
    """UI 启动形态：-c config.ini 必须被接受（此前退出码 2 的根因）。"""
    monkeypatch.setattr(sys, "argv",
                        ["wechat_monitor.py", "-c", "config.ini"])
    args = parse_args()
    assert args.config == "config.ini"


def test_long_config_flag_accepted(monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["wechat_monitor.py", "--config", "other.ini"])
    assert parse_args().config == "other.ini"


def test_config_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wechat_monitor.py"])
    assert parse_args().config == "config.ini"


def test_bad_flag_exits_2(monkeypatch):
    """未知参数仍应以退出码 2 拒绝（argparse 标准语义，防回归误改）。"""
    monkeypatch.setattr(sys, "argv", ["wechat_monitor.py", "-x"])
    with pytest.raises(SystemExit) as exc:
        parse_args()
    assert exc.value.code == 2


# ---- 配置节归属回归（2026-09-09 实测事故）----
# [data_source] 节加入 config.ini 时，service_a_url/service_a_token 被挤到
# 文件尾部落入 data_source 节；load_config 只从 [monitor] 读 → 静默得到空
# 值 → 推送器未启用 → 只落盘不推送、永不出 PDF。

def _write_ini(tmp_path, layout):
    ini = tmp_path / "config.ini"
    if layout == "correct":
        ini.write_text(
            "[monitor]\ntarget_groups = 真英雄\n"
            "service_a_url = http://127.0.0.1:16320\n"
            "service_a_token = tok\n\n"
            "[data_source]\nsource = local\n", encoding="utf-8")
    else:  # orphaned：键被挤进 data_source 节
        ini.write_text(
            "[monitor]\ntarget_groups = 真英雄\n\n"
            "[data_source]\nsource = local\n"
            "service_a_url = http://127.0.0.1:16320\n"
            "service_a_token = tok\n", encoding="utf-8")
    return ini


def test_service_a_url_read_from_monitor_section(tmp_path, monkeypatch):
    from wechat_monitor import load_config
    for env in ("WECHAT_SERVICE_A_URL", "WECHAT_SERVICE_A_TOKEN",
                "WECHAT_TARGET_GROUPS"):
        monkeypatch.delenv(env, raising=False)
    cfg = load_config(str(_write_ini(tmp_path, "correct")))
    assert cfg.service_a_url == "http://127.0.0.1:16320"
    assert cfg.service_a_token == "tok"


def test_repo_config_has_push_enabled():
    """仓库 config.ini 必须使推送生效（防 [data_source] 节再挤压键位）。"""
    from pathlib import Path
    from wechat_monitor import load_config
    repo_ini = Path(__file__).resolve().parent.parent / "config.ini"
    cfg = load_config(str(repo_ini))
    assert cfg.service_a_url, "config.ini 的 service_a_url 未被读取——"
    "检查该键是否仍在 [monitor] 节内"
