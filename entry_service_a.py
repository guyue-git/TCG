"""打包入口：service_a（python -m service_a 的等价 exe 入口）.

相对导入要求包上下文，故经包内 __main__.main() 调用；
PyInstaller Analysis 以本文件为脚本入口，pathex 含项目根，
service_a 包可正常导入。
"""

from __future__ import annotations

import sys

from service_a.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
