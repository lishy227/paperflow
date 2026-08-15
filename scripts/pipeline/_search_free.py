#!/usr/bin/env python3
"""
_search_free.py — 免费源搜索（OpenAlex）
================================================

纯 API 搜索，无需 key、无需浏览器、无需会话。
输出与 _search_scopus.py 相同的契约：{database, total_results, count, papers[]}。

用法:
    python _search_free.py --db openalex --url "<查询 URL>" --target 40 --output out.json


免费源说明:
    - OpenAlex: 综合 2.5 亿+文献，自带引文数/venue/OA 标记；摘要为 inverted index 需重建


退出码:
    0  成功（即使结果少于 target）
    1  可重试失败（网络/API 错误）
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/
from utils.encoding import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

UA = "paperflow/1.0 (free-source research demo)"
TIMEOUT = 30


# ─────────────────────────────────────────────────────────────
# 通用
# ─────────────────────────────────────────────────────────────

def http_get(url: str, retries: int = 2) -> str:
    """GET 文本，带简单重试（网络抖动/5xx）。失败抛异常。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def rebuild_abstract(inverted_index: dict | None) -> str:
    """OpenAlex abstract_inverted_index → 正常摘要文本。"""
    if not inverted_index:
        return ""
    positions = {}
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def strip_arxiv_version(arxiv_id: str) -> str:
    """2301.12345v2 → 2301.12345（缓存主键用无版本 ID）。"""
    return re.sub(r"v\d+$", "", arxiv_id.strip())


# ─────────────────────────────────────────────────────────────
# OpenAlex adapter
# ─────────────────────────────────────────────────────────────

def search_openalex(url: str, target: int, year_range: str = "") -> dict:
    raw = http_get(url)
    data = json.loads(raw)
    meta = data.get("meta", {})
    total = str(meta.get("count", "?"))
    works = data.get("results", [])

    papers = []
    for w in works:
        title = (w.get("display_name") or "").strip()
        if not title:
            continue

        # 作者
        authors = []
        for a in (w.get("authorships") or [])[:20]:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                authors.append(name)

        # DOI（去掉 https://doi.org/ 前缀）
        doi = (w.get("doi") or "").strip()
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        # venue
        venue = ""
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        venue = (src.get("display_name") or "").strip()

        # OpenAlex ID → Wxxxxx
        oa_id = ""
        oa_full = w.get("id") or ""
        m = re.search(r"(W\d+)$", oa_full)
        if m:
            oa_id = m.group(1)

        # arXiv ID（OpenAlex 收录的预印本）
        # 优先从 arXiv DOI 反推（10.48550/arxiv.xxxx → xxxx），最稳定；
        # 兜底从 locations 找 arXiv 源（display_name 形如 "arXiv (Cornell University)"，
        # 不能 == "arxiv" 精确匹配——历史 bug：全空）。
        arxiv_id = ""
        m = re.match(r"10\.48550/arxiv\.([^/]+)", doi, re.IGNORECASE)
        if m:
            arxiv_id = strip_arxiv_version(m.group(1))
        else:
            for loc in (w.get("locations") or []):
                s = loc.get("source") or {}
                if "arxiv" in (s.get("display_name") or "").lower():
                    lp = loc.get("landing_page_url") or ""
                    m2 = re.search(r"arxiv\.org/abs/([^/]+)", lp)
                    if m2:
                        arxiv_id = strip_arxiv_version(m2.group(1))
                    break

        papers.append({
            "title": title,
            "authors": "; ".join(authors),
            "year": str(w.get("publication_year") or ""),
            "venue": venue,
            "doi": doi,
            "citations": int(w.get("cited_by_count") or 0),
            "link": f"https://doi.org/{doi}" if doi else f"https://openalex.org/{oa_id}",
            "type": (w.get("type") or ""),
            "abstract": rebuild_abstract(w.get("abstract_inverted_index")),
            "openaccess": bool((w.get("open_access") or {}).get("is_oa")),
            "openalex_id": oa_id,
            "arxiv_id": arxiv_id,
            "_source_db": ["openalex"],
        })
        if len(papers) >= target:
            break

    return {"database": "openalex", "total_results": total, "count": len(papers),
            "pages_fetched": 1, "papers": papers}


# ─────────────────────────────────────────────────────────────


ADAPTERS = {"openalex": search_openalex}


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="免费源搜索（OpenAlex，纯 API）")
    parser.add_argument("--db", required=True, choices=list(ADAPTERS.keys()))
    parser.add_argument("--url", required=True, help="query_builder 生成的查询 URL")
    parser.add_argument("--target", type=int, default=40)
    parser.add_argument("--year", default="", help="年份范围（兼容参数，openalex 用 URL filter 过滤）")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args()

    try:
        result = ADAPTERS[args.db](args.url, args.target, args.year)
    except Exception as e:
        sys.stderr.write(f"[{args.db}] 搜索失败: {type(e).__name__}: {e}\n")
        print(f"DONE|{args.db}|0|ERROR")
        return 1

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    sys.stderr.write(f"[{args.db}] {result['count']} papers (total: {result['total_results']}) -> {out}\n")
    if result["count"] == 0:
        print(f"DONE|{args.db}|0|EMPTY")
        return 0
    print(f"DONE|{args.db}|{result['count']}|ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
