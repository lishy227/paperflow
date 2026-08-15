"""
Tier 2 详情页摘要提取 — 批量打开详情页，评估 extractor JS，写缓存文件。
用法:
  python _tier2_extract.py --tasks memory/tier2_task.json --extractors-dir extractors/ --output-dir memory/paper-abstracts/
"""

import sys, os, json, time, argparse, random, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.env_config import get_cdp_base, load_dotenv
from utils.cache_utils import write_cache as _write_cache, cache_filename as _cache_filename
load_dotenv()

try:
    import websocket
except ImportError:
    print("缺少 websocket-client 依赖: 请先运行 pip install -r requirements.txt", file=sys.stderr)
    sys.exit(2)

CDP = get_cdp_base()
PAGE_TIMEOUT = 9
EVAL_TIMEOUT = 15


def _cdp_new_tab():
    """Create a new browser tab via CDP. Returns {id, webSocketDebuggerUrl}."""
    req = urllib.request.Request(f"{CDP}/json/new", method="PUT")
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def _cdp_close_tab(target_id):
    """Close a tab by target id."""
    try:
        req = urllib.request.Request(f"{CDP}/json/close/{target_id}", method="GET")
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _navigate_and_wait(ws, url, wait_spa=5):
    """Navigate a tab and wait for Page.loadEventFired + SPA render."""
    ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
    ws.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
    ws.send(json.dumps({"id": 3, "method": "Page.navigate", "params": {"url": url}}))

    deadline = time.time() + PAGE_TIMEOUT
    loaded = False
    while time.time() < deadline:
        ws.settimeout(3)
        try:
            data = json.loads(ws.recv())
            if data.get("method") == "Page.loadEventFired":
                loaded = True
                break
        except (websocket.WebSocketTimeoutException, json.JSONDecodeError,
                ConnectionResetError, BrokenPipeError):
            pass

    # SPA 可能永不触发 loadEventFired——超时也继续，靠调用方 sleep 等待渲染
    time.sleep(wait_spa)
    return True


def _evaluate_extractor(ws, js_code, timeout=EVAL_TIMEOUT):
    """Evaluate extractor JS. Returns (result_dict, error)."""
    msg = {
        "id": 100,
        "method": "Runtime.evaluate",
        "params": {
            "expression": js_code,
            "returnByValue": True,
            "awaitPromise": True,
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


def _write_abstract_cache(output_dir, db, doc_id, result):
    """Write extracted abstract to cache file (统一走 cache_utils.write_cache)。"""
    paper = {
        "title": result.get("title", ""),
        "doi": result.get("doi", ""),
        "docId": result.get("docId", "") or doc_id or "",
        "_source_db": [db],
    }
    abstract = result.get("abstract", "") if isinstance(result, dict) else str(result)
    extra = {"authors": result.get("authors", ""),
             "year": result.get("year", ""),
             "venue": result.get("venue", ""),
             "citations": result.get("citations", "")}
    filename = _write_cache(paper, abstract, output_dir, extra)
    return os.path.join(output_dir, filename) if filename else None


def extract_batch(papers, extractors_dir, output_dir, batch_size, wait_spa):
    """
    Extract abstracts for a batch of papers.
    Returns (ok_count, failed_list, per_paper_results).

    All databases use per-paper detail page navigation.
    REST API fast paths (Scopus gateway, EV /rest/doc) removed for safety.
    """

    # NOTE: Scopus/EV REST API fast paths removed.
    # All papers now go through per-paper detail page navigation:
    #   navigate article page 鈫?wait SPA 鈫?evaluate extractor 鈫?write cache
    # This is slower but has complete Referer chains and Sec-Fetch-Mode: navigate,
    # making traffic indistinguishable from normal human browsing.

    tabs = []  # [(paper_index, ws, target_id, extractor_js)]

    # Phase 1: Open tabs and navigate
    for i, paper in enumerate(papers):
        link = paper.get("link", "")
        if not link:
            sys.stderr.write(f"  [{i}] skip: no link\n")
            continue

        # Get extractor JS
        extractor_path = paper.get("detail_extractor", "")
        if not extractor_path:
            db = paper.get("source_db", "unknown")
            extractor_path = f"{db}_detail.js"

        # Resolve path
        if not os.path.isabs(extractor_path):
            extractor_path = os.path.join(extractors_dir, os.path.basename(extractor_path))

        if not os.path.exists(extractor_path):
            sys.stderr.write(f"  [{i}] skip: extractor not found: {extractor_path}\n")
            continue

        with open(extractor_path, "r", encoding="utf-8") as f:
            extractor_js = f.read()

        sys.stderr.write(f"  [{i}] Opening: {paper.get('title', '?')[:60]}...\n")

        try:
            tab = _cdp_new_tab()
            ws_url = tab.get("webSocketDebuggerUrl")
            target_id = tab.get("id", "")
            if not ws_url:
                sys.stderr.write(f"  [{i}] failed to get ws_url\n")
                continue

            ws = websocket.create_connection(ws_url, timeout=PAGE_TIMEOUT, suppress_origin=True)

            if not _navigate_and_wait(ws, link, wait_spa=0):  # Don't wait SPA yet
                sys.stderr.write(f"  [{i}] navigation failed\n")
                try:
                    ws.close()
                except:
                    pass
                if target_id:
                    _cdp_close_tab(target_id)
                continue

            tabs.append((i, ws, target_id, extractor_js, paper))

            # Space out tab opens to avoid burst traffic to same DB
            time.sleep(random.uniform(2.5, 4.0))

        except Exception as e:
            sys.stderr.write(f"  [{i}] error: {e}\n")
            continue

    if not tabs:
        early_fail = [(p.get("title", "?"), "no tabs opened") for p in papers]
        return 0, early_fail, []

    # Phase 2: Wait for SPA render on all tabs (with jitter to avoid fixed patterns)
    wait_with_jitter = random.uniform(wait_spa * 0.85, wait_spa * 1.15)
    sys.stderr.write(f"  Waiting {wait_with_jitter:.1f}s for SPA render on {len(tabs)} tabs...\n")
    time.sleep(wait_with_jitter)

    # Phase 3: Evaluate extractors and write results
    ok = 0
    failed = []
    per_paper = []

    for i, ws, target_id, extractor_js, paper in tabs:
        try:
            result, err = _evaluate_extractor(ws, extractor_js)
            if err:
                sys.stderr.write(f"  [{i}] extract error: {err}\n")
                failed.append((paper.get("title", "?"), err))
                per_paper.append({
                    "title": paper.get("title", ""),
                    "doi": paper.get("doi", ""),
                    "docId": paper.get("docId", ""),
                    "source_db": paper.get("source_db", "unknown"),
                    "abstract": "",
                    "status": "failed",
                    "reason": str(err)[:200],
                })
            elif result is None:
                sys.stderr.write(f"  [{i}] extract returned None\n")
                failed.append((paper.get("title", "?"), "null result"))
                per_paper.append({
                    "title": paper.get("title", ""),
                    "doi": paper.get("doi", ""),
                    "docId": paper.get("docId", ""),
                    "source_db": paper.get("source_db", "unknown"),
                    "abstract": "",
                    "status": "failed",
                    "reason": "null result",
                })
            else:
                abstract = result.get("abstract", "") if isinstance(result, dict) else str(result)
                if abstract and len(str(abstract).strip()) >= 50:
                    doc_id = result.get("docId", "") if isinstance(result, dict) else ""
                    db = paper.get("source_db", "unknown")
                    cache_file = _write_abstract_cache(output_dir, db, doc_id, result)
                    if not cache_file:
                        failed.append((paper.get("title", "?"), "write_cache returned None"))
                        continue
                    sys.stderr.write(f"  [{i}] OK: {len(str(abstract))} chars 鈫?{os.path.basename(cache_file)}\n")
                    ok += 1
                    per_paper.append({
                        "title": paper.get("title", ""),
                        "doi": result.get("doi", paper.get("doi", "")),
                        "docId": doc_id or paper.get("docId", ""),
                        "source_db": db,
                        "abstract": str(abstract).strip(),
                        "status": "ok",
                    })
                else:
                    sys.stderr.write(f"  [{i}] abstract too short: {len(str(abstract))}\n")
                    failed.append((paper.get("title", "?"), "abstract too short"))
                    per_paper.append({
                        "title": paper.get("title", ""),
                        "doi": paper.get("doi", ""),
                        "docId": paper.get("docId", ""),
                        "source_db": paper.get("source_db", "unknown"),
                        "abstract": str(abstract).strip() if abstract else "",
                        "status": "failed",
                        "reason": "abstract too short",
                    })
        except Exception as e:
            sys.stderr.write(f"  [{i}] exception: {e}\n")
            failed.append((paper.get("title", "?"), str(e)[:200]))
            per_paper.append({
                "title": paper.get("title", ""),
                "doi": paper.get("doi", ""),
                "docId": paper.get("docId", ""),
                "source_db": paper.get("source_db", "unknown"),
                "abstract": "",
                "status": "failed",
                "reason": str(e)[:200],
            })
        finally:
            try:
                ws.close()
            except:
                pass
            if target_id:
                _cdp_close_tab(target_id)

        # Small random pause between evaluations (avoid metronomic pattern)
        time.sleep(random.uniform(0.3, 1.0))

    return ok, failed, per_paper


def main():
    p = argparse.ArgumentParser(description="Tier 2 detail page abstract extraction")
    p.add_argument("--tasks", required=True, help="path to tier2_task.json")
    p.add_argument("--extractors-dir", required=True, help="directory containing *_detail.js extractors")
    p.add_argument("--output-dir", required=True, help="directory for paper-abstracts cache files")
    p.add_argument("--batch-size", type=int, default=7, help="max tabs per batch")
    p.add_argument("--wait", type=int, default=10, help="seconds to wait for SPA render after all tabs navigated")
    args = p.parse_args()

    # Resolve paths relative to cwd
    tasks_path = args.tasks
    if not os.path.isabs(tasks_path):
        tasks_path = os.path.join(os.getcwd(), tasks_path)

    with open(tasks_path, "r", encoding="utf-8") as f:
        task = json.load(f)

    papers = task.get("papers", [])
    total = len(papers)
    sys.stderr.write(f"Tier 2: {total} papers to extract\n")

    # Group by source_db to batch same extractor together
    total_ok = 0
    total_failed = []
    all_per_paper = []

    for batch_start in range(0, total, args.batch_size):
        batch_end = min(batch_start + args.batch_size, total)
        batch = papers[batch_start:batch_end]
        sys.stderr.write(f"\nBatch {batch_start // args.batch_size + 1}: papers {batch_start}-{batch_end - 1}\n")

        ok, failed, per_paper = extract_batch(batch, args.extractors_dir, args.output_dir,
                                   args.batch_size, args.wait)
        total_ok += ok
        total_failed.extend(failed)
        all_per_paper.extend(per_paper)

    # Write per-paper result (for merge_tier2.py)
    result_path = os.path.join(os.path.dirname(tasks_path), "tier2_result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(all_per_paper, f, ensure_ascii=False, indent=2)

    # Print summary for sub-agent reporting
    failed_titles = [t[:60] for t, _ in total_failed]
    print(f"DONE|tier2|{total_ok} ok|{len(total_failed)} failed")
    if total_failed:
        print(f"Failed: {failed_titles}")


if __name__ == "__main__":
    main()

