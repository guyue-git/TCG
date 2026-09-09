"""打包入口：运维管理台（ops_ui）.

windowed exe 下 sys.stdout 为 NullWriter，logging 照常工作（静默）；
--selftest 模式构建窗口 800ms 后自退（与源码态语义一致）。
"""

from __future__ import annotations

from ops_ui.main import main

if __name__ == "__main__":
    main()
