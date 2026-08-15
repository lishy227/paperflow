#!/usr/bin/env python3
"""
cdp_utils.py — CDP 标签页工具（standalone 版）
============================================

统一管理"需要一个标签页"的场景：
    - 优先复用已有的 about:blank 空闲标签（避免反复新建导致空页堆积）
    - 没有才新建

被 _health_check.py（会话检查）等使用。
"""

import json
import time
import urllib.request
from urllib.request import Request

from utils.env_config import get_cdp_base


def list_tabs(cdp_base: str = None) -> list[dict]:
    """列出浏览器所有标签页（CDP /json/list）。失败返回 []。"""
    base = cdp_base or get_cdp_base()
    try:
        with urllib.request.urlopen(f"{base}/json/list", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []


def _cdp_new_tab(cdp_base: str = None) -> dict:
    """新建一个空白标签页（about:blank）。返回 {id, webSocketDebuggerUrl}。"""
    base = cdp_base or get_cdp_base()
    req = Request(f"{base}/json/new", method="PUT")
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode("utf-8"))


def _cdp_close_tab(target_id: str, cdp_base: str = None) -> None:
    """关闭指定标签页。失败静默（不抛异常）。

    重试一次：Chrome 忙时 /json/close 偶发失败（健康检查曾观察到 tab 残留）。
    """
    base = cdp_base or get_cdp_base()
    for attempt in range(2):
        try:
            req = Request(f"{base}/json/close/{target_id}", method="GET")
            urllib.request.urlopen(req, timeout=5)
            return
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
    # 两次失败也静默（调用方不依赖关闭结果）


def get_or_create_blank_tab(cdp_base: str = None) -> dict:
    """复用已有 about:blank 空闲标签，没有才新建。

    返回 {id, webSocketDebuggerUrl}。调用方用完应 close_tab()；
    即使关闭失败，下次检查也会复用同一个空页，不会无限堆积。
    """
    for t in list_tabs(cdp_base):
        if t.get("type") == "page" and t.get("url", "").strip() in ("about:blank", ""):
            return {"id": t["id"], "webSocketDebuggerUrl": t["webSocketDebuggerUrl"]}
    return _cdp_new_tab(cdp_base)


def close_tab(target_id: str, cdp_base: str = None) -> None:
    """关闭标签页（别名，语义清晰）。"""
    _cdp_close_tab(target_id, cdp_base)


def close_all_blank_tabs(cdp_base: str = None) -> int:
    """关闭所有 about:blank 空闲标签页。返回关闭数量。"""
    base = cdp_base or get_cdp_base()
    closed = 0
    for t in list_tabs(base):
        if t.get("type") == "page" and t.get("url", "").strip() in ("about:blank", ""):
            _cdp_close_tab(t["id"], base)
            closed += 1
    return closed


if __name__ == "__main__":
    n = close_all_blank_tabs()
    print(f"closed {n} blank tab(s)")
