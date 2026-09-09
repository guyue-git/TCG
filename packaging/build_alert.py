# -*- coding: utf-8 -*-
"""统一构建入口：PyInstaller 打包 + 渲染内核整目录复制（方案 A 产物根）.

用法（项目根或任意目录均可）：
    python packaging/build_alert.py [产物目录名]

产物目录名缺省为 alert（覆盖式更新，构建前请按惯例备份运行数据）；
传入其他名称（如 alert_20260908_local）则并行构建新目录，
不影响正在运行的现网 dist/alert。

为什么不用 spec Tree：PyInstaller onedir 的 COLLECT 会把所有 TOC 目标
强制放入 _internal/，无法表达「产物根」布局；而 frozen 态渲染内核定位
（edge_locator._candidate_from_bundled_shell）按 base_dir=exe 目录探测，
故内核必须在 dist/alert/chrome-headless-shell/，只能构建后复制。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

PACK = Path(__file__).resolve().parent
ROOT = PACK.parent
DIST = PACK / "dist"
SHELL_SRC = PACK / "chrome-headless-shell"
SHELL_EXE_NAME = "chrome-headless-shell.exe"


def main() -> int:
    # 产物目录名：argv[1] 可选（缺省 alert = 原行为；传入新名称 = 并行构建）
    out_name = sys.argv[1] if len(sys.argv) > 1 else "alert"
    if out_name != "alert" and not re.fullmatch(r"[A-Za-z0-9_.-]+", out_name):
        print(f"[FAIL] 非法产物目录名: {out_name!r}")
        return 1
    out_dir = DIST / out_name
    shell_dst = out_dir / "chrome-headless-shell"

    if not (SHELL_SRC / SHELL_EXE_NAME).is_file():
        print(f"[FAIL] 未找到渲染内核源目录: {SHELL_SRC}\\{SHELL_EXE_NAME}")
        print("请从 experiments/poc_headless_shell/shell/"
              "chrome-headless-shell-win64 整目录复制"
              "（约 270MB，含 dll/pak/locales，不可只拷 exe）。")
        return 1

    cmd = [sys.executable, "-m", "PyInstaller", str(PACK / "alert.spec"),
           "--noconfirm", "--distpath", str(DIST),
           "--workpath", str(PACK / "build")]
    if out_name != "alert":
        # 并行构建：临时 distpath 承接 PyInstaller 输出（现网 dist/alert
        # 被运行中的进程锁定，且不可覆盖），完成后再整体搬入新目录
        tmp_dist = PACK / "dist_stage"
        if tmp_dist.exists():
            shutil.rmtree(tmp_dist)
        cmd += ["--distpath", str(tmp_dist)]
    print("[build] PyInstaller:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    if out_name != "alert":
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.move(str(tmp_dist / "alert"), str(out_dir))
        try:
            tmp_dist.rmdir()
        except OSError:
            pass
        print(f"[build] 产物已迁移至独立目录: {out_dir}")

    if shell_dst.exists():
        shutil.rmtree(shell_dst)
    shutil.copytree(SHELL_SRC, shell_dst)
    print(f"[build] 渲染内核已复制: {shell_dst}")

    # 两份 ini 拷贝到产物根（仅缺失时；不覆盖目标机已调好的配置）
    for ini in ("config.ini", "service_a_config.ini"):
        src = ROOT / ini
        dst = out_dir / ini
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            print(f"[build] ini 已复制（目标机缺失）: {dst.name}")
    print(f"[build] 完成: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
