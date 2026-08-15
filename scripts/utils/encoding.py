#!/usr/bin/env python3
"""
encoding.py — 统一 stdout/stderr 编码（standalone 版）
=====================================================

Windows 下 Python 输出编码跟随 console code page（中文系统 = GBK），
带 ✓/✗ 等非 GBK 字符的 print 会抛 UnicodeEncodeError。
所有入口脚本（main.py / health_check.py / launch_browser.py）在 import 后
立即调用 ensure_utf8_stdio()，统一按 UTF-8 输出（配合 bat 的 chcp 65001）。
"""

import sys


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 重配为 UTF-8（失败静默）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = getattr(stream, "encoding", None)
            if enc and enc.lower() not in ("utf-8", "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
