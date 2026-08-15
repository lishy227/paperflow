"""
paperflow 搜索脚本 — 单库串行翻页提取 v3
新增: --ws-url 复用已有 tab, 修复 ACM 分页偏移, EV 提取可靠性

用法:
  # 传统模式：脚本自己开 tab + 导航
  python _search_one.py --db ieee --url "https://..." --extractor extractors/ieee.js --target 40 --wait 12

  # 复用模式：主会话 browser.open(url) → 拿到 wsUrl → 传进来
  python _search_one.py --db ieee --url "..." --extractor extractors/ieee.js --target 40 --ws-url "ws://..." --wait 3
"""

import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.env_config import get_cdp_base, load_dotenv
load_dotenv()

try:
    import websocket
except ImportError:
    print("缺少 websocket-client 依赖: 请先运行 pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)

CDP = get_cdp_base()
PAGE_TIMEOUT = 30
EVAL_TIMEOUT = 20


def _cdp_new_tab():
    import urllib.request
    req = urllib.request.Request(f"{CDP}/json/new", method="PUT")
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _cdp_close_tab(target_id):
    """Close a browser tab by target id."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{CDP}/json/close/{target_id}", method="GET")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _cdp_navigate(ws, url):
    """Navigate, waiting for Page.loadEventFired if it fires.
    SPA (ACM/EV/IEEE) 可能永不触发 loadEventFired，超时也返回 True，
    交给调用方的 sleep(space_wait) 等待渲染。"""
    ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
    ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
    ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": url}}))
    deadline = time.time() + PAGE_TIMEOUT
    while time.time() < deadline:
        ws.settimeout(3)
        try:
            data = json.loads(ws.recv())
            if data.get("method") == "Page.loadEventFired":
                return True
        except (websocket.WebSocketTimeoutException, json.JSONDecodeError,
                ConnectionResetError, BrokenPipeError):
            pass
    return True


def _cdp_navigate_no_wait(ws, url):
    """Navigate without waiting for loadEventFired (fire-and-forget for SPA)."""
    ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
    ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
    ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": url}}))
    # Don't wait for loadEventFired — SPA might not fire it reliably.
    # Caller must sleep(space_wait) after.
    return True


def _cdp_evaluate(ws, js_code, timeout=EVAL_TIMEOUT):
    """Evaluate JS, return (result_dict, error)."""
    msg = {
        "id": 100,
        "method": "Runtime.evaluate",
        "params": {
            "expression": js_code,
            "returnByValue": True,
            "awaitPromise": False,
        },
    }
    ws.send(json.dumps(msg))
    deadline = time.time() + timeout
    while time.time() < deadline:
        ws.settimeout(3)
        try:
            data = json.loads(ws.recv())
            if data.get("id") == 100:
                r = data.get("result", {}).get("result", {})
                if r.get("type") == "object":
                    return r.get("value"), None
                if r.get("type") == "string":
                    return r.get("value"), None
                return None, f"unexpected result type: {r.get('type')}"
            if data.get("method") == "Runtime.exceptionThrown":
                exc = data.get("params", {}).get("exceptionDetails", {})
                return None, exc.get("text", str(exc))[:300]
        except websocket.WebSocketTimeoutException:
            pass
        except Exception as e:
            return None, str(e)[:200]
    return None, "timeout"


def _cdp_click(ws, selector):
    """Click an element via evaluate on the same ws connection."""
    js = f'(function(){{var e=document.querySelector({json.dumps(selector)});if(e){{e.click();return true;}}return false;}})()'
    result, err = _cdp_evaluate(ws, js, timeout=5)
    return result is True


def search_one_page(url, extractor_js, space_wait, ws_url=None, skip_navigate=False):
    """Open/reuse tab, navigate, wait, extract. Returns (result_dict, ws_url).
    If ws_url given, reuses that tab. If skip_navigate, does not change URL."""
    ws = None
    if ws_url:
        ws = websocket.create_connection(ws_url, timeout=PAGE_TIMEOUT, suppress_origin=True)
        # Always enable Runtime on reuse (necessary for some SPA cases)
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        if not skip_navigate:
            # Use no-wait navigate: SPA page transitions don't fire Page.loadEventFired
            _cdp_navigate_no_wait(ws, url)
        time.sleep(space_wait)
        result, err = _cdp_evaluate(ws, extractor_js)
        if err:
            sys.stderr.write(f"  extract error: {err}\n")
            try: ws.close()
            except: pass
            return None, None
        # Don't close ws — caller may reuse via ws_url
        return result, ws_url
    else:
        tab = _cdp_new_tab()
        target_id = tab.get("id", "")
        ws_url = tab.get("webSocketDebuggerUrl")
        if not ws_url:
            _cdp_close_tab(target_id)
            return None, None
        ws = websocket.create_connection(ws_url, timeout=PAGE_TIMEOUT, suppress_origin=True)
        try:
            if not _cdp_navigate(ws, url):
                return None, None
            time.sleep(space_wait)
            result, err = _cdp_evaluate(ws, extractor_js)
            if err:
                sys.stderr.write(f"  extract error: {err}\n")
                return None, None
            return result, None
        finally:
            try: ws.close()
            except: pass
            _cdp_close_tab(target_id)


def do_extract(ws_url, extractor_js, space_wait):
    """Extract papers from current page state (no navigation)."""
    ws = websocket.create_connection(ws_url, timeout=PAGE_TIMEOUT, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        time.sleep(space_wait)
        result, err = _cdp_evaluate(ws, extractor_js)
        if err:
            sys.stderr.write(f"  extract error: {err}\n")
            return None
        return result
    finally:
        try: ws.close()
        except: pass


def search(db, url, extractor_path, output_path, target, space_wait, paginate, param, first_value, search_query=None, ws_url=None):
    with open(extractor_path, "r", encoding="utf-8") as f:
        extractor_js = f.read()

    all_papers = []
    total_results = "?"
    max_pages = 10
    shared_ws_url = ws_url  # Use provided ws_url if given
    is_reuse_mode = bool(ws_url)
    shared_target_id = ""  # Track for cleanup

    # EV form fill: only if we're NOT reusing a pre-loaded tab
    if search_query and db == "ev" and not is_reuse_mode:
        ev_fill_js = (
            "(function(){"
            "var inp=document.querySelector('input[name=\"search-first\"]');"
            "if(!inp)return 'no_input';"
            "var ns=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;"
            "ns.call(inp," + json.dumps(search_query) + ");"
            "inp.dispatchEvent(new Event('input',{bubbles:true}));"
            "inp.dispatchEvent(new Event('change',{bubbles:true}));"
            "setTimeout(function(){"
            "var btn=Array.from(document.querySelectorAll('button.MuiButton-root[type=\"submit\"]'))"
            ".find(function(b){return b.textContent.trim()==='Search'&&!b.closest('.ot-sdk-container');});"
            "if(btn)btn.click();"
            "},300);"
            "return 'ok';"
            "})()"
        )
        sys.stderr.write("  [ev] Opening search form...\n")
        tab = _cdp_new_tab()
        shared_target_id = tab.get("id", "")
        shared_ws_url = tab.get("webSocketDebuggerUrl")
        if not shared_ws_url:
            sys.stderr.write("  [ev] Failed to create tab\n")
        else:
            r, _ = search_one_page(url, ev_fill_js, space_wait, ws_url=shared_ws_url)
            if r:
                sys.stderr.write(f"  [ev] Search submitted, waiting for results...\n")
                time.sleep(space_wait)
            else:
                shared_ws_url = None

    # --- Pagination loop ---
    page_param_value = first_value  # The actual pagination parameter value
    page_num = 1  # Human-readable page number

    for it in range(max_pages):
        # Build page URL
        page_url = url
        if paginate == "url_param" and it > 0:
            sep = "&" if "?" in url else "?"
            page_url = f"{url}{sep}{param}={page_param_value}"

        sys.stderr.write(f"  Fetching page {page_num} (param {param}={page_param_value})...\n")

        if is_reuse_mode:
            # Reuse mode: extract without navigation on first page,
            # navigate on subsequent pages (using same ws)
            if it == 0:
                # First page: page already loaded by main session, just extract
                result = do_extract(shared_ws_url, extractor_js, space_wait)
            else:
                # Subsequent pages: navigate on same ws, then extract
                result, _ = search_one_page(page_url, extractor_js, space_wait, ws_url=shared_ws_url)
        elif shared_ws_url and (paginate == "click" or db == "ev"):
            # Legacy EV mode: reuse tab after form fill or click
            skip_nav = (db == "ev" and it == 0)
            result, _ = search_one_page(page_url, extractor_js, space_wait, ws_url=shared_ws_url, skip_navigate=skip_nav)
        else:
            # Standard mode: new tab per page
            result, _ = search_one_page(page_url, extractor_js, space_wait)

        if result is None:
            sys.stderr.write(f"  Page {page_num}: navigation/extraction failed\n")
            # For reuse mode, try one retry with fresh tab
            if is_reuse_mode and it > 0:
                sys.stderr.write(f"  Page {page_num}: retrying with fresh tab...\n")
                time.sleep(3)
                result, _ = search_one_page(page_url, extractor_js, space_wait)
                if result is None:
                    break
            else:
                break

        count = result.get("count", 0)
        if count == 0:
            sys.stderr.write(f"  Page {page_num}: empty, stopping\n")
            break

        total_results = result.get("totalResults", total_results)
        papers = result.get("papers", [])
        all_papers.extend(papers)
        sys.stderr.write(f"  Page {page_num}: {len(papers)} papers (total: {len(all_papers)})\n")

        if len(all_papers) >= target or len(papers) < 25:
            break

        # --- Pagination advance ---
        if paginate == "click" and shared_ws_url:
            ws = websocket.create_connection(shared_ws_url, timeout=10, suppress_origin=True)
            clicked = _cdp_click(ws, "#next-page-top, .next-page-top, a.pagination-next")
            try: ws.close()
            except: pass
            if not clicked:
                sys.stderr.write(f"  No next page button, stopping\n")
                break
            sys.stderr.write(f"  Clicked next page, waiting {space_wait}s...\n")
            time.sleep(space_wait)
        else:
            page_param_value += 1
            page_num += 1
            if paginate == "url_param":
                time.sleep(2)

    # Close shared tab (only if we created it, not in reuse mode)
    if shared_target_id and not is_reuse_mode:
        _cdp_close_tab(shared_target_id)

    output = {
        "count": len(all_papers),
        "total_results": total_results,
        "pages_fetched": page_num,
        "database": db,
        "papers": all_papers,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    if len(all_papers) == 0:
        print(f"DONE|{db}|0|PAGE_EMPTY")
    else:
        print(f"DONE|{db}|{len(all_papers)}|ok")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--extractor", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--target", type=int, default=40)
    p.add_argument("--wait", type=int, default=12, help="seconds to wait for SPA render after loadEvent")
    p.add_argument("--paginate", default="url_param", choices=["url_param", "click", "none"])
    p.add_argument("--param", default="pageNumber")
    p.add_argument("--first", type=int, default=1, help="first value of pagination parameter (0 for ACM startPage)")
    p.add_argument("--query", default=None, help="EV search query (fills form via evaluate)")
    p.add_argument("--ws-url", default=None, help="WebSocket URL of existing tab (skip new tab creation)")
    args = p.parse_args()
    search(args.db, args.url, args.extractor, args.output, args.target, args.wait,
           args.paginate, args.param, args.first, args.query, args.ws_url)
