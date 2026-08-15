#!/usr/bin/env python3
"""
health_check.py — paperflow 统一健康检查（可随时调用）
=========================================================

检查四类：
    1. 依赖库（yaml / websocket / aiohttp）
    2. Scopus API key（.env 的 SCOPUS_API_KEY）
    3. 浏览器 CDP（127.0.0.1:CDP_PORT 是否在线）
    4. 扩展数据库会话（IEEE / Scopus / ACM / EV，复用 pipeline/_health_check.py）

用法：
    python scripts/health_check.py            # 人类可读输出
    python scripts/health_check.py --json     # JSON 输出（机器可读）
    python scripts/health_check.py --db acm   # 只查单个库的会话

被调用方：
    - main.py 管道 Stage 0（stage_health）
    - open-databases.bat 一键脚本末尾
    - 手动随时检查

退出码：
    0  全部通过
    1  可重试失败（会话失效 / 浏览器未启动）
    2  环境/配置错误（缺依赖 / 缺 key）
"""

import argparse
import json
import os
import sys

# 保证任何入口都能 import 到 scripts/ 下的包
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SCRIPTS)
for _p in (_ROOT, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.env_config import get_cdp_base, get_scopus_api_key  # noqa: E402
from pipeline._health_check import check_all, check_one, HEALTH_TESTS  # noqa: E402
from utils.encoding import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

REQUIRED_MODULES = ["yaml", "websocket", "aiohttp"]

# 免费源（纯 API，无需 key/浏览器/会话）
FREE_DBS = {"openalex"}


def check_deps() -> dict:
    """依赖库检查。"""
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        return {"ok": False, "msg": f"缺少依赖: {', '.join(missing)} — 运行: pip install -r requirements.txt"}
    return {"ok": True, "msg": "依赖库齐全"}


def check_scopus_key() -> dict:
    """Scopus API key 配置检查（只查配置，不调 API——调 API 归会话检查）。"""
    key = get_scopus_api_key()
    if not key:
        return {"ok": False, "msg": "未配置 SCOPUS_API_KEY — 编辑 .env"}
    return {"ok": True, "msg": "SCOPUS_API_KEY 已配置"}


def check_browser() -> dict:
    """浏览器 CDP 在线检查。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{get_cdp_base()}/json/version", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "msg": f"CDP 在线 ({data.get('Browser', '?')})"}
    except Exception:
        return {"ok": False, "msg": f"浏览器未启动（{get_cdp_base()} 无响应）— 运行: python launch_browser.py"}


def run_all(databases: list | None = None) -> dict:
    """跑全部检查，返回统一报告 dict。

    databases: 任务选择的数据库列表。全部为免费源（openalex）时
               → 免费源模式：跳过 key/浏览器/会话检查（纯 API 不需要）。
               None / 空 / 含扩展数据库 → 全查（默认行为）。

    结构:
        {
          "ok": bool,
          "checks": {
            "deps": {"ok": bool, "msg": str},
            "scopus_key": {"ok": bool, "msg": str},
            "browser": {"ok": bool, "msg": str},
            "logins": {"ok": bool, "msg": str, "detail": {db: {ok, msg}}},
          },
          "exit_code": 0|1|2,
          "summary": str,
        }
    """
    dbs = databases or []
    free_only = bool(dbs) and all(d in FREE_DBS for d in dbs)

    checks = {"deps": check_deps()}

    if free_only:
        checks["scopus_key"] = {"ok": True, "msg": "免费源模式，无需 SCOPUS_API_KEY"}
        checks["browser"] = {"ok": True, "msg": "免费源模式，无需浏览器（纯 API）"}
        checks["logins"] = {"ok": True, "msg": f"免费源模式（{', '.join(sorted(dbs))}），跳过会话检查", "detail": {}}
    else:
        checks["scopus_key"] = check_scopus_key()
        checks["browser"] = check_browser()

        # 环境类检查（缺依赖/缺 key/浏览器没启动）不通过 → 不继续查会话
        env_failed = [k for k in ("deps", "scopus_key", "browser") if not checks[k]["ok"]]

        if env_failed:
            checks["logins"] = {"ok": False, "msg": "环境检查未通过，跳过会话检查", "detail": {}}
        else:
            # 源相关：只查任务实际选用的扩展数据库（dbs 里过滤掉免费源）；
            # 未指定（None/空）→ 全查 config 里 enabled 的库
            paid_dbs = [d for d in dbs if d not in FREE_DBS] or None
            login_report = check_all(databases=paid_dbs)
            checks["logins"] = {
                "ok": login_report["passed"],
                "msg": login_report["summary"],
                "detail": login_report["checks"],
            }

    env_checks = [k for k in ("deps", "scopus_key", "browser") if not checks[k]["ok"]]
    all_ok = all(v["ok"] for v in checks.values())
    if env_checks:
        exit_code = 2
    elif all_ok:
        exit_code = 0
    else:
        exit_code = 1

    if free_only:
        summary = "免费源模式（无需账号）: " + ("依赖✓" if checks["deps"]["ok"] else "依赖✗")
    else:
        summary_parts = []
        if checks["deps"]["ok"]:
            summary_parts.append("依赖✓")
        else:
            summary_parts.append("依赖✗")
        if checks["scopus_key"]["ok"]:
            summary_parts.append("key✓")
        else:
            summary_parts.append("key✗")
        if checks["browser"]["ok"]:
            summary_parts.append("浏览器✓")
        else:
            summary_parts.append("浏览器✗")
        if not free_only and "logins" in checks and checks["logins"].get("detail"):
            lp = checks["logins"]
            summary_parts.append(("会话✓" if lp["ok"] else f"会话✗"))
        summary = "全部正常" if all_ok else "；".join(summary_parts) + " — 见上方明细"

    return {
        "ok": all_ok,
        "checks": checks,
        "exit_code": exit_code,
        "summary": summary,
    }


def print_report(report: dict) -> None:
    """人类可读输出。--db 单查模式只有 logins 段，其余段按存在性打印。"""
    checks = report["checks"]
    print("\n=== paperflow 健康检查 ===")
    if "deps" in checks:
        print(f"  [deps]    {'✓' if checks['deps']['ok'] else '✗'} {checks['deps']['msg']}")
    if "scopus_key" in checks:
        print(f"  [key]     {'✓' if checks['scopus_key']['ok'] else '✗'} {checks['scopus_key']['msg']}")
    if "browser" in checks:
        print(f"  [browser] {'✓' if checks['browser']['ok'] else '✗'} {checks['browser']['msg']}")
    logins = checks.get("logins") or {}
    if logins.get("detail"):
        for db_key, chk in logins["detail"].items():
            name = HEALTH_TESTS.get(db_key, {}).get("name", db_key)
            print(f"  [login]   {'✓' if chk.get('ok') else '✗'} {name}: {chk.get('msg', '')}")
    else:
        print(f"  [login]   {'✓' if logins['ok'] else '✗'} {logins['msg']}")
    print("=" * 40)
    print(f"  结果: {'✓ ' if report['ok'] else '✗ '}{report['summary']}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="paperflow 统一健康检查")
    parser.add_argument("--db", help="只检查指定数据库会话 (ieee/scopus/ev/acm/engineering_village)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.db:
        # 兼容 config key 全名
        db_key = "ev" if args.db == "engineering_village" else args.db
        result = check_one(db_key)
        report = {
            "ok": result["ok"],
            "checks": {"logins": {"ok": result["ok"], "msg": result["msg"], "detail": {db_key: result}}},
            "exit_code": 0 if result["ok"] else 1,
            "summary": f"{db_key}: {'✓' if result['ok'] else '✗'} {result['msg']}",
        }
    else:
        report = run_all()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
