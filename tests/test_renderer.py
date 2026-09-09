"""单元测试：渲染层（k_subscript 过滤器 + HTML 输出，不依赖 Edge）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from markupsafe import Markup

from tc_generator.renderer import k_subscript, render_tc_html
from tc_generator.tc_builder import build_tc_fields
from tc_generator.master_file import get_structure_attributes
from tc_generator.parser import parse_order_line

import datetime as dt


def _make_9080_fields():
    order = parse_order_line("#下单 9080 300017 网宿科技 限价 买入 200W")
    attrs = get_structure_attributes("CP001", "9080")
    return build_tc_fields(order, 15.3, dt.date(2026, 8, 21), attrs,
                           "CP001", 1)


class TestKSubscriptFilter:
    def test_standalone_k1_k2_become_subscript(self):
        assert k_subscript("reaches K1: S = min (K1, unwind)") == Markup(
            "reaches K<sub>1</sub>: S = min (K<sub>1</sub>, unwind)")
        assert k_subscript("K2 < S ≤ E") == Markup("K<sub>2</sub> &lt; S ≤ E")

    def test_ratio_tokens_untouched(self):
        text = "adjust K1_ratio to (91% − n%) and K2_ratio to (90% − n%)"
        result = k_subscript(text)
        assert "K1_ratio" in result
        assert "K2_ratio" in result
        assert "<sub>" not in result

    def test_mixed_text(self):
        text = "reaching K1, Issuer shall adjust K1_ratio to (81% − n%)"
        result = k_subscript(text)
        assert "reaching K<sub>1</sub>" in result
        assert "K1_ratio" in result

    def test_escapes_html(self):
        assert "<script>" not in str(k_subscript("<script>K1</script>"))

    def test_plain_k_untouched(self):
        assert k_subscript('Strike Price ("K")') == Markup(
            "Strike Price (&#34;K&#34;)")


class TestRenderedHtml:
    def make_fields(self):
        order = parse_order_line("#下单 9080 300017 网宿科技 限价 买入 200W")
        attrs = get_structure_attributes("CP001", "9080")
        return build_tc_fields(order, 15.3, dt.date(2026, 8, 21), attrs,
                               "CP001", 1)

    def test_html_contains_subscript_k(self):
        html = render_tc_html(self.make_fields())
        # 表头引号内的独立 K1/K2 为下标
        assert "(&#34;K<sub>1</sub>&#34;)" in html or "(“K<sub>1</sub>”)" in html
        assert "(“K<sub>2</sub>”)" in html
        # 追保条款中的独立 K1/K2 为普通下标
        assert "reaching K<sub>1</sub>" in html
        # 结算计算/免责声明中的独立 K 为 .kbig（9.96pt）+ 下标 6.48pt
        assert 'reaches <span class="kbig">K<sub>1</sub></span>' in html
        assert 'reaches <span class="kbig">K<sub>2</sub></span>' in html
        assert '<span class="kbig">K<sub>2</sub></span>: S =' in html
        # 免责声明段落包含 kbig
        disclaimer_part = html.split('class="disclaimer"')[1]
        assert '<span class="kbig">K<sub>1</sub></span>' in disclaimer_part
        # K1_ratio / K2_ratio 保持正常数字
        assert "K1_ratio" in html and "K2_ratio" in html
        assert "K<sub>1</sub>_ratio" not in html
        assert "K<sub>2</sub>_ratio" not in html

    def test_strict_undefined_rejects_missing_field(self):
        with pytest.raises(Exception):
            render_tc_html({"confirmation_number": "X"})


class TestPageBreakBorderCSS:
    """跨页断口封线（2026-09-07）：separate+clone 与 0 高 thead 结构.

    背景：Edge/Chromium 对 border-collapse:collapse 的行分片不画断口边框；
    方案 H（experiments/pagebreak_exp/H_combo.pdf 实证）改为 separate 模型
    td 克隆边框 + 0 高度 thead 跨页重复补顶线。此处断言模板结构不被回退。
    """

    HTML: str = ""

    @classmethod
    def setup_class(cls):
        cls.fields = _make_9080_fields()
        cls.html = render_tc_html(cls.fields)

    def test_base_css_uses_separate_clone_model(self):
        assert "border-collapse: separate" in self.html
        assert "border-spacing: 0" in self.html
        assert "box-decoration-break: clone" in self.html
        assert "-webkit-box-decoration-break: clone" in self.html

    def test_base_css_adjacent_border_dedup(self):
        """去重规则：右邻去左线、后行去顶线、tbody 首行去顶线。"""
        assert "table.tc-table tr td + td { border-left: none; }" in self.html
        assert "table.tc-table tr + tr td { border-top: none; }" in self.html
        assert ("table.tc-table thead + tbody tr td { border-top: none; }"
                in self.html)

    def test_base_css_zero_height_thead(self):
        """0 高 thead：不可见 + 仅保留底边作续页封线。"""
        assert "table.tc-table thead th {" in self.html
        css_block = self.html.split(
            "table.tc-table thead th {")[1].split("}")[0]
        for decl in ("height: 0", "font-size: 0", "border: none",
                     "border-bottom: 0.75pt solid #000"):
            assert decl in css_block

    def test_table_has_zero_height_thead_before_first_row(self):
        """thead 在 colgroup 之后、首个数据行之前（跨页重复的前提）。"""
        head = self.html.split("<table class=\"tc-table\">")[1]
        thead_pos = head.find("<thead><tr><th colspan=\"2\"></th></tr></thead>")
        first_tr = head.find("<tr>")
        assert 0 <= thead_pos < first_tr

    def test_rows_wrapped_in_tbody(self):
        """数据行显式包裹 tbody（thead+tbody 相邻选择器才能命中）。"""
        assert "<tbody>" in self.html and "</tbody>" in self.html


class TestCompulsorySettlementGap:
    """强制结算条目间空行（2026-09-08）：90100 敲出结构的
    Compulsory Settlement Provisions 两条目之间按样例保留一个空行。"""

    def test_90100_blank_line_between_items(self):
        order = parse_order_line("#下单 90100 688825 长鑫科技 限价 买入 160W")
        attrs = get_structure_attributes("CP001", "90100")
        fields = build_tc_fields(order, 58.144, dt.date(2026, 8, 24), attrs,
                                 "CP001", 1, counterparty_name="长鑫科技")
        html = render_tc_html(fields, template_name="tc_90100.html.j2")
        # 条目 1 的 </p> 与条目 2 的 <p> 之间以 <br> 空行分隔
        assert '</p><br><p class="comp-item">' in html


class TestRenderPdfRetry:
    """渲染内核偶发失败的退避重试（不依赖真实内核）.

    2026-09-08 内核切换：假内核桩参数化为 msedge.exe /
    chrome-headless-shell.exe，全用例双内核各跑一遍——统一最小参数集
    裁决下 renderer 不按内核分叉，桩差异仅验证存在性预检与透传。
    """

    KERNEL_STUBS = ("msedge.exe", "chrome-headless-shell.exe")

    @pytest.fixture(params=KERNEL_STUBS, ids=["edge", "headless-shell"])
    def kernel_stub(self, request, tmp_path):
        """renderer 现做存在性预检：假内核桩必须真实存在。"""
        exe = tmp_path / request.param
        exe.write_bytes(b"")
        return str(exe)

    def make_fields(self):
        order = parse_order_line("#下单 9080 300017 网宿科技 限价 买入 200W")
        attrs = get_structure_attributes("CP001", "9080")
        return build_tc_fields(order, 15.3, dt.date(2026, 8, 21), attrs,
                               "CP001", 1)

    @staticmethod
    def _pdf_path_from_cmd(cmd: list[str]) -> Path:
        arg = next(a for a in cmd if a.startswith("--print-to-pdf="))
        return Path(arg.split("=", 1)[1])

    def _install_fake_kernel(self, monkeypatch, outcomes: list[str]):
        """按 outcomes 顺序模拟渲染内核：'fail'=无产物，'ok'=产出 PDF.

        同时 mock 掉失败诊断/清理（快照与残留清理会做真实系统查询），
        单测不依赖宿主机进程状态；清理调用按序记录在 self.cleanup_calls。
        """
        import subprocess as sp
        from tc_generator import renderer
        calls: list[Path] = []
        self.cleanup_calls: list[Path] = []
        monkeypatch.setattr(renderer, "_edge_process_snapshot",
                            lambda: "无渲染内核进程")

        def fake_cleanup(profile):
            self.cleanup_calls.append(Path(profile))
            return "无残留进程"

        monkeypatch.setattr(renderer, "_kill_leftover_by_profile",
                            fake_cleanup)

        def fake_run(cmd, **kwargs):
            pdf_path = self._pdf_path_from_cmd(cmd)
            calls.append(pdf_path)
            outcome = outcomes[len(calls) - 1] if len(calls) <= len(outcomes) \
                else outcomes[-1]
            if outcome == "fail":
                return sp.CompletedProcess(cmd, 1, "", "kernel crashed")
            pdf_path.write_bytes(b"%PDF-1.4 fake")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(renderer.subprocess, "run", fake_run)
        monkeypatch.setattr(renderer.time, "sleep", lambda s: None)
        return calls

    def test_retry_succeeds_after_first_failure(
            self, tmp_path, monkeypatch, kernel_stub):
        # 首败后成：第 2 次尝试产出 PDF，最终交付
        from tc_generator import renderer
        calls = self._install_fake_kernel(monkeypatch, ["fail", "ok"])
        out = tmp_path / "out.pdf"
        result = renderer.render_tc_pdf(self.make_fields(), out,
                                        edge_path=kernel_stub)
        assert len(calls) == 2
        assert result == out
        assert out.read_bytes() == b"%PDF-1.4 fake"

    def test_retry_succeeds_after_timeout(
            self, tmp_path, monkeypatch, kernel_stub):
        # 首次超时（TimeoutExpired）同样触发重试
        import subprocess as sp
        from tc_generator import renderer
        calls: list[Path] = []
        self.cleanup_calls: list[Path] = []
        monkeypatch.setattr(
            renderer, "_kill_leftover_by_profile",
            lambda profile: (self.cleanup_calls.append(Path(profile)),
                             "无残留进程")[1])

        def fake_run(cmd, **kwargs):
            pdf_path = self._pdf_path_from_cmd(cmd)
            calls.append(pdf_path)
            if len(calls) == 1:
                raise sp.TimeoutExpired(cmd, 60)
            pdf_path.write_bytes(b"%PDF-1.4 fake")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(renderer.subprocess, "run", fake_run)
        monkeypatch.setattr(renderer.time, "sleep", lambda s: None)
        out = tmp_path / "out.pdf"
        renderer.render_tc_pdf(self.make_fields(), out, edge_path=kernel_stub)
        assert len(calls) == 2
        # 超时的那次尝试也必须清理残留
        assert len(self.cleanup_calls) == 1

    def test_all_attempts_fail_raises(
            self, tmp_path, monkeypatch, kernel_stub):
        # 三连败：抛出最后一次的 RuntimeError，且不产出目标文件
        from tc_generator import renderer
        calls = self._install_fake_kernel(
            monkeypatch, ["fail", "fail", "fail"])
        out = tmp_path / "out.pdf"
        with pytest.raises(RuntimeError, match="渲染内核未产出 PDF"):
            renderer.render_tc_pdf(self.make_fields(), out,
                                   edge_path=kernel_stub)
        assert len(calls) == 3  # 首跑 + 2 次重试
        assert not out.exists()

    def test_error_message_uses_snapshot_not_count(
            self, tmp_path, monkeypatch, kernel_stub):
        # 最终失败：错误信息只带命令行级快照，不再带误导性进程计数
        # （2026-09-08 目标机实测 tasklist 计数返回 0 与快照矛盾，已删）
        from tc_generator import renderer
        self._install_fake_kernel(monkeypatch, ["fail", "fail", "fail"])
        monkeypatch.setattr(
            renderer, "_edge_process_snapshot",
            lambda: "共 47 个: PID=1 CMD=msedge --no-startup-window")
        out = tmp_path / "out.pdf"
        with pytest.raises(RuntimeError) as exc_info:
            renderer.render_tc_pdf(self.make_fields(), out,
                                   edge_path=kernel_stub)
        msg = str(exc_info.value)
        assert "共 47 个" in msg and "--no-startup-window" in msg
        assert "检测到" not in msg      # 计数措辞已移除
        assert "Edge 进程" not in msg   # 中性措辞，兼容双内核

    def test_cleanup_invoked_per_failed_attempt(
            self, tmp_path, monkeypatch, kernel_stub):
        # 每次失败尝试后立即按该次 profile 特征清理残留进程树
        from tc_generator import renderer
        self._install_fake_kernel(monkeypatch, ["fail", "fail", "ok"])
        out = tmp_path / "out.pdf"
        renderer.render_tc_pdf(self.make_fields(), out, edge_path=kernel_stub)
        assert [p.name for p in self.cleanup_calls] == [
            "edge_profile_1", "edge_profile_2"]

    def test_kernel_cmd_uses_isolated_profile(
            self, tmp_path, monkeypatch, kernel_stub):
        # 每次渲染注入一次性 --user-data-dir（与既有实例隔离）
        import subprocess as sp
        from tc_generator import renderer
        cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            pdf_path = self._pdf_path_from_cmd(cmd)
            cmds.append(list(cmd))
            pdf_path.write_bytes(b"%PDF-1.4 fake")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(renderer.subprocess, "run", fake_run)
        monkeypatch.setattr(renderer.time, "sleep", lambda s: None)
        out = tmp_path / "out.pdf"
        renderer.render_tc_pdf(self.make_fields(), out, edge_path=kernel_stub)
        assert len(cmds) == 1
        udd = [a for a in cmds[0] if a.startswith("--user-data-dir=")]
        assert len(udd) == 1, f"cmd 缺少 --user-data-dir: {cmds[0]}"
        assert udd[0].split("=", 1)[1]  # profile 路径非空；随临时目录自动清理

    def test_each_retry_gets_fresh_profile(
            self, tmp_path, monkeypatch, kernel_stub):
        # 三次尝试各自用独立 profile（edge_profile_1/2/3）——失败的尝试
        # 可能留下半初始化 profile 与残留句柄，重试不得进入污染现场
        import subprocess as sp
        from tc_generator import renderer
        cmds: list[list[str]] = []
        monkeypatch.setattr(renderer, "_kill_leftover_by_profile",
                            lambda profile: "无残留进程")

        def fake_run(cmd, **kwargs):
            pdf_path = self._pdf_path_from_cmd(cmd)
            cmds.append(list(cmd))
            if len(cmds) < 3:
                return sp.CompletedProcess(cmd, 1, "", "fail")
            pdf_path.write_bytes(b"%PDF-1.4 fake")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(renderer.subprocess, "run", fake_run)
        monkeypatch.setattr(renderer.time, "sleep", lambda s: None)
        out = tmp_path / "out.pdf"
        renderer.render_tc_pdf(self.make_fields(), out, edge_path=kernel_stub)
        profiles = [next(a.split("=", 1)[1] for a in c
                         if a.startswith("--user-data-dir="))
                    for c in cmds]
        assert len(profiles) == 3
        assert len(set(profiles)) == 3, f"profile 未逐次更换: {profiles}"

    def test_failure_message_contains_stdout_and_snapshot(
            self, tmp_path, monkeypatch, kernel_stub):
        # 最终失败：错误信息带 stdout（内核版本差异可能写 stdout）
        # 以及每个渲染内核进程的命令行快照（当场定位干扰源）
        import subprocess as sp
        from tc_generator import renderer

        def fake_run(cmd, **kwargs):
            return sp.CompletedProcess(cmd, 0, "stdout-clue", "stderr-clue")

        def fake_snapshot():
            return "共 2 个: PID=1 CMD=--no-startup-window; PID=2 CMD=x"

        monkeypatch.setattr(renderer.subprocess, "run", fake_run)
        monkeypatch.setattr(renderer.time, "sleep", lambda s: None)
        monkeypatch.setattr(renderer, "_edge_process_snapshot", fake_snapshot)
        monkeypatch.setattr(renderer, "_kill_leftover_by_profile",
                            lambda profile: "无残留进程")
        out = tmp_path / "out.pdf"
        with pytest.raises(RuntimeError) as exc_info:
            renderer.render_tc_pdf(self.make_fields(), out,
                                   edge_path=kernel_stub)
        msg = str(exc_info.value)
        assert "stdout-clue" in msg and "stderr-clue" in msg
        assert "共 2 个" in msg and "--no-startup-window" in msg

    def test_no_retry_when_first_succeeds(
            self, tmp_path, monkeypatch, kernel_stub):
        from tc_generator import renderer
        calls = self._install_fake_kernel(monkeypatch, ["ok"])
        out = tmp_path / "out.pdf"
        renderer.render_tc_pdf(self.make_fields(), out, edge_path=kernel_stub)
        assert len(calls) == 1


class TestLeftoverProcessTreeCleanup:
    """失败清理：真实进程集成验证（非 mock）.

    复现 launcher 分离僵死场景：残留进程命令行含本次尝试独有的
    --user-data-dir 特征、映像名为 chrome-headless-shell.exe（复制的
    cmd.exe 桩），render 失败路径应将其 taskkill /T /F。
    """

    def test_failure_kills_leftover_process_tree(self, tmp_path):
        import shutil
        import subprocess as sp
        import time
        from tc_generator import renderer

        cmd_src = shutil.which("cmd.exe")
        assert cmd_src, "测试前置：找不到 cmd.exe"
        profile_dir = tmp_path / "edge_profile_1"
        profile_dir.mkdir()
        stub = tmp_path / "chrome-headless-shell.exe"
        shutil.copy2(cmd_src, stub)
        # 命令行注入 user-data-dir 特征串 + ping 保活 30s（timeout 需控制台
        # stdin 会立即退出），模拟僵死渲染进程
        proc = sp.Popen(
            [str(stub), "/c", "echo", f"--user-data-dir={profile_dir}",
             "&", "ping", "-n", "30", "127.0.0.1", ">nul"],
            creationflags=sp.CREATE_NO_WINDOW)
        try:
            assert proc.poll() is None, "测试前置：桩进程应存活"
            result = renderer._kill_leftover_by_profile(profile_dir)
            assert "已清理进程树" in result, result
            deadline = time.time() + 10
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            assert proc.poll() is not None, "残留进程未被 taskkill 清理"
        finally:
            if proc.poll() is None:
                proc.kill()
