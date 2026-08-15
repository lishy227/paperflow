"""
paid-db-access 登录态健康检查
在 Stage 0 管道启动前运行，验证四个数据库的浏览器 session 是否有效。

用法:
  python _health_check.py                  # 检查所有启用的库
  python _health_check.py --db ieee        # 只检查指定库
  python _health_check.py --json           # JSON 输出（供管道调用）

每个库的测试都是最小化的固定测试：
  - IEEE:    打开搜索页 → 检查 xplGlobal 是否定义（未登录 = 重定向到登录页）
  - Scopus:  检查 API key 是否有效（HTTP 直接请求，不走浏览器）
  - EV:      打开搜索页 → 检查搜索表单是否存在（未登录 = 重定向到登录页）
  - ACM:     打开搜索页 → 检查搜索结果是否返回（未登录 = 重定向到登录页）

输出格式 (--json):
  {
    "passed": true/false,
    "checks": {
      "ieee": {"ok": true, "msg": "xplGlobal 已加载，登录态正常"},
      "scopus": {"ok": true, "msg": "API key 有效"},
      "ev": {"ok": false, "msg": "页面重定向到登录页"},
      "acm": {"ok": true, "msg": "搜索结果正常返回"}
    },
    "failed_count": 1,
    "summary": "1/4 数据库登录态异常: EV"
  }
"""

import sys, os, json, time, argparse, urllib.request, urllib.error, ssl

# ---- 硬编码的测试参数（固定小测试） ----

HEALTH_TESTS = {
    "ieee": {
        "name": "IEEE Xplore",
        "url": "https://ieeexplore.ieee.org/search/searchresult.jsp?queryText=test",
        "wait_s": 12,
        "check_js": """
            (() => {
                try {
                    const body = document.body ? document.body.innerText : '';
                    const hasGlobal = typeof xplGlobal !== 'undefined' && xplGlobal !== null;
                    const hasResults = /Showing[\\s\\S]{0,80}of\\s*[\\d,]+/.test(body) && /Results?/i.test(body);
                    const signedIn = body.includes('Sign Out') || body.includes('Access provided by');
                    const isLoginPage = body.includes('Sign In') && body.includes('Institutional');
                    if (hasGlobal || signedIn || hasResults) {
                        const how = signedIn ? '机构访问已登录' : (hasGlobal ? 'xplGlobal 已加载' : '搜索结果已返回');
                        return JSON.stringify({ok: true, msg: how + '，登录态正常'});
                    }
                    if (isLoginPage) {
                        return JSON.stringify({ok: false, msg: '页面重定向到登录页，需要重新登录'});
                    }
                    return JSON.stringify({ok: false, msg: '无法确认登录态，请手动检查浏览器的 IEEE 页面'});
                } catch(e) {
                    return JSON.stringify({ok: false, msg: '检查脚本执行异常: ' + e.message});
                }
            })()
        """,
    },
    "scopus": {
        "name": "Scopus",
        # Scopus 搜索用 API key，不依赖浏览器 session
        # 用最轻量的 API 调用验证 key 有效性
        "api_check": True,
    },
    "ev": {
        "name": "Engineering Village",
        "url": "https://www.engineeringvillage.com/app/search/quick",
        "wait_s": 8,
        "check_js": """
            (() => {
                try {
                    const body = document.body ? document.body.innerText : '';
                    const href = window.location.href || '';
                    const hasSearchForm = document.querySelector('input[type="search"], input[placeholder*="earch"], .search-input') !== null;
                    const redirectedToLogin = href.includes('id.elsevier.com') || href.includes('authorization') || href.includes('prompt=login');
                    const welcomePage = href.includes('home.url') || body.includes('Trouble signing in') || body.includes('Welcome to Engineering Village');
                    const isLoginPage = body.includes('Sign in') || body.includes('Login');
                    if (hasSearchForm && !redirectedToLogin) {
                        return JSON.stringify({ok: true, msg: '搜索页加载成功，登录态正常'});
                    }
                    if (redirectedToLogin || isLoginPage || welcomePage) {
                        return JSON.stringify({ok: false, msg: '未登录（跳转到 Elsevier 登录或欢迎页），需要重新登录'});
                    }
                    return JSON.stringify({ok: false, msg: '无法确认登录态: 未找到搜索表单也未检测到登录页。请手动检查'});
                } catch(e) {
                    return JSON.stringify({ok: false, msg: '检查脚本执行异常: ' + e.message});
                }
            })()
        """,
    },
    "acm": {
        "name": "ACM Digital Library",
        "url": "https://dl.acm.org/action/doSearch?AllField=test&pageSize=3",
        "wait_s": 8,
        "check_js": """
            (() => {
                try {
                    const body = document.body ? document.body.innerText : '';
                    const hasResults = /of\\s*[\\d,]+\\s*Results?/i.test(body);
                    const isLoginPage = body.includes('Sign In') || body.includes('sign in');
                    const isCloudflare = body.includes('安全验证') || body.includes('cloudflare') ||
                                         body.includes('Ray ID') || body.includes('Just a moment') ||
                                         body.includes('verify you are human') || body.includes('请稍候');
                    if (hasResults) {
                        return JSON.stringify({ok: true, msg: '搜索结果正常返回，登录态正常'});
                    }
                    if (isCloudflare) {
                        return JSON.stringify({ok: false, msg: '触发 Cloudflare 人机验证，请在浏览器里完成验证后重试'});
                    }
                    if (isLoginPage) {
                        return JSON.stringify({ok: false, msg: '页面重定向到登录页，需要重新登录'});
                    }
                    return JSON.stringify({ok: false, msg: '无法确认登录态: 无搜索结果也无登录提示。请手动检查'});
                } catch(e) {
                    return JSON.stringify({ok: false, msg: '检查脚本执行异常: ' + e.message});
                }
            })()
        """,
    },
}


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.env_config import get_cdp_base, get_scopus_api_key, load_dotenv
from utils.cdp_utils import get_or_create_blank_tab, close_tab
load_dotenv()

CDP = get_cdp_base()


def _cdp_new_tab():
    """Deprecated: use utils.cdp_utils.get_or_create_blank_tab (复用空闲空页)."""
    from utils.cdp_utils import _cdp_new_tab as _impl
    return _impl()


def _cdp_close_tab(target_id):
    """Deprecated: use utils.cdp_utils.close_tab."""
    close_tab(target_id)


def _cdp_evaluate(ws_url, js_code, timeout_s=10):
    """Evaluate JS via CDP WebSocket, return (result_json_str_or_None, error_str_or_None)."""
    import websocket
    ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {"expression": js_code, "returnByValue": True, "awaitPromise": False},
        }))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            ws.settimeout(3)
            try:
                msg = json.loads(ws.recv())
                if msg.get("id") == 2:
                    result = msg.get("result", {})
                    if "result" in result:
                        return result["result"].get("value", None), None
                    if "exceptionDetails" in result:
                        return None, result["exceptionDetails"].get("text", "CDP exception")
                    return None, f"CDP error: {result.get('error', 'unknown')}"
            except (websocket.WebSocketTimeoutException, json.JSONDecodeError,
                    ConnectionResetError, BrokenPipeError):
                pass
        return None, "Timeout waiting for evaluate result"
    finally:
        ws.close()


def _check_scopus_api(config_path="config.yaml"):
    """Check Scopus API key validity with a minimal query."""
    # standalone 版：.env 的 SCOPUS_API_KEY 优先，config.yaml 兜底
    api_key = get_scopus_api_key()
    if not api_key:
        return {"ok": False, "msg": "未配置 Scopus API key（.env 的 SCOPUS_API_KEY）"}

    # 最小查询：返回 1 篇，不获取任何字段
    url = f"https://api.elsevier.com/content/search/scopus?query=test&count=1&apiKey={api_key}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    try:
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        if resp.status == 200:
            return {"ok": True, "msg": "API key 有效"}
        return {"ok": False, "msg": f"API 返回 HTTP {resp.status}"}
    except urllib.error.HTTPError as e:
        if e.code == 401 or e.code == 403:
            return {"ok": False, "msg": f"API key 无效或权限不足 (HTTP {e.code})"}
        return {"ok": False, "msg": f"API 请求失败 (HTTP {e.code})"}
    except Exception as e:
        return {"ok": False, "msg": f"API 请求异常: {e}"}


def check_one(db_key, config_path="config.yaml"):
    """Check one database. Returns {"ok": bool, "msg": str}."""
    # 兼容 config key 全名（engineering_village）与健康检查短名（ev）
    if db_key == "engineering_village":
        db_key = "ev"
    test = HEALTH_TESTS.get(db_key)
    if not test:
        return {"ok": False, "msg": f"未知数据库: {db_key}"}

    name = test["name"]

    # Scopus: API key check (no browser needed)
    if test.get("api_check"):
        return _check_scopus_api(config_path)

    # Browser-based check
    if CDP is None:
        return {"ok": False, "msg": "浏览器未连接 (CDP 不可用)"}

    target_id = None
    ws_url = None
    try:
        # 每库独立新建 tab（并行安全：线程各自持有独立 tab，互不干扰；用完即关）
        tab = _cdp_new_tab()
        target_id = tab["id"]
        ws_url = tab.get("webSocketDebuggerUrl", "")

        # Navigate
        ws = __import__("websocket").create_connection(ws_url, timeout=15, suppress_origin=True)
        try:
            ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
            ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
            ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": test["url"]}}))
            # 不等 loadEventFired：SPA/Cloudflare 页面可能永不触发，空转 30s 是主要耗时来源。
            # 导航发出后直接进固定等待，靠 evaluate 的 JS 特征判断（含 Cloudflare/登录页检测），
            # 页面未就绪时也会明确报"无法确认"而非误判正常。
        finally:
            ws.close()

        # SPA 渲染等待：每个库用各自配置的 wait_s（ACM/EV 是 SPA，2s 不够）
        time.sleep(test.get("wait_s", 6))

        # Evaluate check
        result, err = _cdp_evaluate(ws_url, test["check_js"])
        if err:
            return {"ok": False, "msg": f"JS 执行失败: {err}"}
        if result is None:
            return {"ok": False, "msg": "JS 执行无返回值"}
        parsed = json.loads(result)
        return parsed

    except Exception as e:
        return {"ok": False, "msg": f"检查异常: {e}"}
    finally:
        if target_id and ws_url:
            _cdp_close_tab(target_id)


def check_all(config_path="config.yaml", databases=None):
    """Check databases. Returns full report dict.

    databases: 显式指定要检查的库（config key 名，如 ieee/scopus/engineering_village/acm）。
               传入 → 只查这些库（源相关）；None → 从 config.yaml 读 enabled 的库。
    """
    if databases:
        enabled = list(databases)
    else:
        # Load config to get enabled DBs
        try:
            import yaml
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            dbs = config.get("databases", {})
            enabled = [k for k, v in dbs.items() if v.get("enabled", False)]
        except Exception:
            # Fallback: check all four
            enabled = ["ieee", "scopus", "ev", "acm"]

    from concurrent.futures import ThreadPoolExecutor

    # Map config keys to health test keys
    keys = []
    for db_key in enabled:
        hk = db_key
        if db_key == "engineering_village":
            hk = "ev"
        if hk in HEALTH_TESTS:
            keys.append(hk)

    # 并行执行：每库独立 tab 同时导航/等待/检查（各库不同域名互不干扰），
    # 总耗时 ≈ 最慢的库（而不是串行相加）；Scopus 走 HTTP 天然无冲突。
    with ThreadPoolExecutor(max_workers=len(keys) or 1) as ex:
        futures = {hk: ex.submit(check_one, hk, config_path) for hk in keys}
        checks = {hk: f.result() for hk, f in futures.items()}

    failed = [k for k, v in checks.items() if not v.get("ok")]
    passed = all(v.get("ok") for v in checks.values())

    scope = "所选数据库" if databases else "所有数据库"
    return {
        "passed": passed,
        "checks": checks,
        "failed_count": len(failed),
        "failed_dbs": failed,
        "summary": (
            f"{scope}登录态正常"
            if passed
            else f"{len(failed)}/{len(checks)} 数据库登录态异常: {', '.join(failed)}"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="paid-db-access 登录态健康检查")
    parser.add_argument("--db", help="只检查指定数据库 (ieee/scopus/ev/acm)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    if args.db:
        result = check_one(args.db, args.config)
        report = {
            "passed": result["ok"],
            "checks": {args.db: result},
            "failed_count": 0 if result["ok"] else 1,
            "summary": f"{args.db}: {'✓' if result['ok'] else '✗'} {result['msg']}",
        }
    else:
        report = check_all(args.config)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        # 人类可读输出
        print("\n" + "=" * 50)
        print("  paid-db-access 登录态健康检查")
        print("=" * 50)
        for db_key, check in report["checks"].items():
            icon = "✓" if check["ok"] else "✗"
            name = HEALTH_TESTS.get(db_key, {}).get("name", db_key)
            print(f"  {icon} {name}: {check['msg']}")
        print("=" * 50)
        print(f"  结果: {report['summary']}")
        print()

    sys.exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
