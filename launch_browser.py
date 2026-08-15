#!/usr/bin/env python3
"""
launch_browser.py — 启动/复用 CDP 浏览器（standalone 版）
========================================================

替代 OpenClaw 的 browser 管理。用独立 user-data-dir 持久化登录态，
关闭再启动后 IEEE/ACM/EV 的登录保持有效（cookie 存磁盘）。

用法：
    python launch_browser.py                 # 检测或启动浏览器，然后退出
    python launch_browser.py --open <url>    # 启动并打开页面（登录引导用）
    python launch_browser.py --status        # 只检查浏览器是否在运行
    python launch_browser.py --kill          # 关闭自己启动的浏览器（profile 保留）

端口冲突处理：
    - 如果 127.0.0.1:18800 已有浏览器在监听 → 直接复用，不重复启动
    - 如果端口被非浏览器占用 → 报错并提示换 CDP_PORT
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from utils.env_config import get_cdp_base, get_cdp_port, project_path, load_dotenv
from utils.encoding import ensure_utf8_stdio

ensure_utf8_stdio()

# 独立 profile 目录（登录态持久化在这里）
PROFILE_DIR = project_path(".browser-profile")
LOG_DIR = project_path(".browser-logs")
LOG_PATH = LOG_DIR / "browser.log"

# Chrome/Edge 常见安装路径（Windows）
BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
]

PERSISTENT_FLAG = ".launched-by-standalone"  # 标记：记录本脚本启动的浏览器


def find_browser() -> str | None:
    """在常见路径里找 Chrome/Edge。找不到返回 None。"""
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def cdp_alive() -> bool:
    """检查 CDP 端口是否有浏览器在监听。"""
    try:
        with urllib.request.urlopen(f"{get_cdp_base()}/json/version", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def cdp_version() -> str:
    """返回 CDP 浏览器的版本描述。"""
    try:
        with urllib.request.urlopen(f"{get_cdp_base()}/json/version", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("Browser", "?")
    except Exception:
        return "?"


def launch(open_urls: list[str] | None = None) -> int:
    """启动浏览器（或复用已有），打开一个或多个页面。返回 0 成功 / 1 失败。"""
    open_urls = open_urls or []
    if cdp_alive():
        print(f"[browser] 已有浏览器在运行: {cdp_version()} @ {get_cdp_base()}")
        for url in open_urls:
            _open_tab(url)
        return 0

    browser = find_browser()
    if not browser:
        print("[browser] ✗ 找不到 Chrome/Edge。请安装 Chrome 或手动配置 BROWER_PATH 环境变量", file=sys.stderr)
        print(f"          已检查: {', '.join(BROWSER_CANDIDATES)}", file=sys.stderr)
        return 1

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser,
        f"--remote-debugging-port={get_cdp_port()}",
        "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-features=Translate,MediaRouter",
        "--window-size=1280,900",
    ]
    # 多 URL 直接给 chrome.exe（原生一次开多 tab）；无 URL 则不开（Chrome 默认新标签页）
    if open_urls:
        cmd.extend(open_urls)

    print(f"[browser] 启动: {browser}")
    print(f"[browser] profile: {PROFILE_DIR}（登录态持久化在这里）")
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as logf:
            subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, shell=False)
        # 标记：记录本次启动（供 --kill 使用）
        (PROFILE_DIR / PERSISTENT_FLAG).write_text("1", encoding="utf-8")
    except Exception as e:
        print(f"[browser] ✗ 启动失败: {e}", file=sys.stderr)
        return 1

    # 轮询等待 CDP 就绪（最多 20 秒）
    print(f"[browser] 等待 CDP 就绪 ({get_cdp_base()})...", end="", flush=True)
    for _ in range(40):
        time.sleep(0.5)
        if cdp_alive():
            print(" ✓")
            print(f"[browser] 就绪: {cdp_version()}")
            return 0
    print(" ✗ 超时")
    print("[browser] 请检查日志: " + str(LOG_PATH), file=sys.stderr)
    return 1


def _open_tab(url: str) -> None:
    """在已有浏览器里打开页面：同站点已有标签页则复用（不重复开），否则新建。"""
    import urllib.parse
    from utils.cdp_utils import list_tabs
    domain = urllib.parse.urlparse(url).netloc
    # 同站点已有 tab → 复用（避免每次 bat 都堆新标签）
    for t in list_tabs():
        if t.get("type") == "page":
            try:
                if urllib.parse.urlparse(t.get("url", "")).netloc == domain:
                    print(f"[browser] 已有标签页（{domain}），复用")
                    return
            except Exception:
                pass
    # 没有 → 新建
    try:
        req = urllib.request.Request(
            f"{get_cdp_base()}/json/new?{urllib.parse.urlencode({'url': url})}",
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[browser] 已打开: {url}")
    except Exception as e:
        print(f"[browser] 开新标签失败（忽略）: {e}", file=sys.stderr)


def kill() -> None:
    """关闭本脚本启动的浏览器（保留 profile，登录态不清除）。"""
    flag = PROFILE_DIR / PERSISTENT_FLAG
    if not flag.exists():
        print("[browser] 没有本脚本启动的浏览器记录，跳过（安全起见不杀第三方进程）")
        return
    try:
        with urllib.request.urlopen(f"{get_cdp_base()}/json/version", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ws_url = data.get("webSocketDebuggerUrl", "")
        if ws_url:
            # 通过 CDP Browser.close 优雅关闭
            import websocket
            ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
            ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
            ws.close()
            print("[browser] 已发送关闭指令")
        flag.unlink(missing_ok=True)
    except Exception as e:
        print(f"[browser] 关闭失败: {e}（可手动关浏览器窗口）", file=sys.stderr)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="paid-db-access 浏览器管理")
    parser.add_argument("--open", action="append", metavar="URL", help="启动后打开的页面（可重复传，一次开多个）")
    parser.add_argument("--status", action="store_true", help="只检查浏览器状态")
    parser.add_argument("--kill", action="store_true", help="关闭本脚本启动的浏览器")
    args = parser.parse_args()

    if args.status:
        if cdp_alive():
            print(f"[browser] 运行中: {cdp_version()} @ {get_cdp_base()}")
            return 0
        print("[browser] 未运行")
        return 1

    if args.kill:
        kill()
        return 0

    return launch(open_urls=args.open or [])


if __name__ == "__main__":
    sys.exit(main())
