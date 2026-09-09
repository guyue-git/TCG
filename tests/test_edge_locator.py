"""Edge 定位器测试（打包/换机环境适配，2026-09-04 审计项 #1-3）."""

from __future__ import annotations

import pytest

from tc_generator import edge_locator as el
from tc_generator.edge_locator import (
    EdgeNotFoundError, ensure_edge_available, locate_edge)


@pytest.fixture(autouse=True)
def _clear_auto_cache():
    """隔离 lru_cache 跨测试污染（测试内按需打桩，不做全局屏蔽）。"""
    el._locate_auto_cached.cache_clear()
    yield
    el._locate_auto_cached.cache_clear()


def _stub_all_discovery_off(monkeypatch):
    """把全部发现渠道打桩为「无候选」（自动发现必失败的最小环境）。"""
    monkeypatch.setattr(el, "_candidate_from_registry", lambda: None)
    monkeypatch.setattr(el, "_candidate_from_common_roots", lambda: None)
    monkeypatch.setattr(el, "_candidate_from_path_env", lambda: None)
    monkeypatch.delenv("EDGE_PATH", raising=False)


def _make_exe(tmp_path, *parts):
    exe = tmp_path.joinpath(*parts)
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"")
    return exe


class TestLocateEdge:

    def test_explicit_path_wins(self, tmp_path):
        exe = _make_exe(tmp_path, "msedge.exe")
        assert locate_edge(str(exe)) == str(exe)

    def test_explicit_empty_triggers_auto(self, monkeypatch, tmp_path):
        exe = _make_exe(tmp_path, "reg", "msedge.exe")
        monkeypatch.setattr(el, "_locate_auto_cached", lambda: str(exe))
        assert locate_edge("") == str(exe)

    def test_env_var_priority_before_roots(self, monkeypatch, tmp_path):
        """EDGE_PATH 命中即返回，不再探测后续渠道。"""
        env_exe = _make_exe(tmp_path, "env", "msedge.exe")
        monkeypatch.setenv("EDGE_PATH", str(env_exe))
        monkeypatch.setattr(el, "_candidate_from_registry", lambda: None)

        def _roots():
            raise AssertionError("不应探测到常见目录（env 已命中）")
        monkeypatch.setattr(el, "_candidate_from_common_roots", _roots)
        assert el._locate_auto() == str(env_exe)

    def test_registry_candidate_used(self, monkeypatch, tmp_path):
        reg_exe = _make_exe(tmp_path, "reg", "msedge.exe")
        monkeypatch.setattr(el, "_candidate_from_registry", lambda: str(reg_exe))
        assert el._locate_auto() == str(reg_exe)

    def test_registry_dead_path_falls_through(self, monkeypatch, tmp_path):
        _stub_all_discovery_off(monkeypatch)
        reg_exe = str(tmp_path / "gone" / "msedge.exe")   # 注册表给了死路径
        monkeypatch.setattr(el, "_candidate_from_registry", lambda: reg_exe)
        common_exe = _make_exe(tmp_path, "roots", "msedge.exe")
        monkeypatch.setattr(el, "_candidate_from_common_roots",
                            lambda: str(common_exe))
        assert el._locate_auto() == str(common_exe)

    def test_common_roots_versioned_layout(self, monkeypatch, tmp_path):
        """版本化布局：<root>\\版本号\\msedge.exe，取最高版本。"""
        monkeypatch.setattr(el, "_candidate_from_registry", lambda: None)
        monkeypatch.delenv("EDGE_PATH", raising=False)
        for ver in ("1.0.0.0", "9.8.7.6", "10.0.1.2"):
            _make_exe(tmp_path, ver, "msedge.exe")
        monkeypatch.setattr(el, "_COMMON_ROOTS", (str(tmp_path),))
        assert el._locate_auto() == str(tmp_path / "10.0.1.2" / "msedge.exe")

    def test_path_env_fallback(self, monkeypatch, tmp_path):
        bin_dir = _make_exe(tmp_path, "bin", "msedge.exe").parent
        monkeypatch.setattr(el, "_candidate_from_registry", lambda: None)
        monkeypatch.setattr(el, "_candidate_from_common_roots", lambda: None)
        monkeypatch.setenv("PATH", str(bin_dir))
        assert el._locate_auto() == str(bin_dir / "msedge.exe")

    def test_not_found_raises_with_guidance(self, monkeypatch):
        _stub_all_discovery_off(monkeypatch)
        with pytest.raises(EdgeNotFoundError, match="edge_path"):
            el._locate_auto()

    def test_auto_discovery_cached(self, monkeypatch, tmp_path):
        """自动发现成功结果缓存：多次调用只扫描一次
        （失败不缓存——服务恢复后下次渲染自动重试）。"""
        exe = _make_exe(tmp_path, "roots", "msedge.exe")
        calls = {"n": 0}

        def _roots():
            calls["n"] += 1
            return str(exe)
        monkeypatch.setattr(el, "_candidate_from_registry", lambda: None)
        monkeypatch.setattr(el, "_candidate_from_common_roots", _roots)
        monkeypatch.setattr(el, "_candidate_from_path_env", lambda: None)
        assert el._locate_auto_cached() == str(exe)
        assert el._locate_auto_cached() == str(exe)
        assert calls["n"] == 1


class TestBundledHeadlessShell:
    """frozen 态随包 headless-shell 自动发现（2026-09-08 内核切换）."""

    @staticmethod
    def _freeze(monkeypatch, base_dir):
        import sys
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            sys, "executable", str(base_dir / "service_a.exe"))

    def test_source_mode_returns_none(self, tmp_path):
        """源码态无「随包」概念，即使目录存在也不参与发现。"""
        _make_exe(tmp_path, "chrome-headless-shell",
                  "chrome-headless-shell.exe")
        assert el._candidate_from_bundled_shell() is None

    def test_frozen_bundled_dir_discovered(self, monkeypatch, tmp_path):
        """方案 A 布局：<exe 目录>\\chrome-headless-shell\\*.exe。"""
        self._freeze(monkeypatch, tmp_path)
        exe = _make_exe(tmp_path, "chrome-headless-shell",
                        "chrome-headless-shell.exe")
        assert el._candidate_from_bundled_shell() == str(exe)

    def test_frozen_flat_layout_discovered(self, monkeypatch, tmp_path):
        """兼容布局：用户把 exe 直接放在产物根。"""
        self._freeze(monkeypatch, tmp_path)
        exe = _make_exe(tmp_path, "chrome-headless-shell.exe")
        assert el._candidate_from_bundled_shell() == str(exe)

    def test_bundled_priority_over_edge_discovery(
            self, monkeypatch, tmp_path):
        """随包内核优先于 Edge 注册表发现（主内核裁决顺序）。"""
        self._freeze(monkeypatch, tmp_path)
        shell_exe = _make_exe(tmp_path, "chrome-headless-shell",
                              "chrome-headless-shell.exe")
        reg_exe = _make_exe(tmp_path, "reg", "msedge.exe")
        monkeypatch.setattr(el, "_candidate_from_registry",
                            lambda: str(reg_exe))
        assert el._locate_auto() == str(shell_exe)


class TestEnsureEdgeAvailable:

    def test_existing_explicit_passes_through(self, tmp_path):
        exe = _make_exe(tmp_path, "msedge.exe")
        assert ensure_edge_available(str(exe)) == str(exe)

    def test_missing_explicit_raises_no_silent_fallback(self):
        """显式配置的路径不存在：明确报错，不悄悄回退自动发现。"""
        with pytest.raises(EdgeNotFoundError, match="不存在"):
            ensure_edge_available("C:/definitely/missing/msedge.exe")

    def test_env_vars_expanded(self, monkeypatch, tmp_path):
        exe = _make_exe(tmp_path, "msedge.exe")
        monkeypatch.setenv("FAKE_EDGE_DIR", str(tmp_path))
        assert ensure_edge_available("%FAKE_EDGE_DIR%\\msedge.exe") == str(exe)
