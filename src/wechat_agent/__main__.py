# -*- coding: utf-8 -*-
"""让 `python -m wechat_agent` 可用。

（只写 main.py 是不够的 —— 包要被 -m 直接执行，必须有 __main__.py。
这个坑第一次实跑就撞上了。）
"""
import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
