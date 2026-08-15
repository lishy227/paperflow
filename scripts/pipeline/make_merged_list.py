#!/usr/bin/env python3
"""
make_merged_list.py — 合并去重后阶段性清单（Stage 2 输出）
=========================================================
在 merge（合并去重 + 标题初筛）之后、enrich（摘要补全）之前生成：
    - 顶部来源统计：每个来源有几篇文章（阶段性统计）
    - 逐篇清单：标题 / 作者 / 年份 / 来源 / 期刊会议 / DOI / 链接

用法:
  python make_merged_list.py -i step2_merge/merged_filtered.json -o step2_merge/merged_list.md
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.encoding import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

DB_NAMES = {
    "openalex": "OpenAlex",
    "scopus": "Scopus",
    "ieee": "IEEE",
    "acm": "ACM",
    "engineering_village": "EV",
    "ev": "EV",
}


def _fmt_authors(authors: str, limit: int = 4) -> str:
    """作者字符串截断：'A; B; C; D; E' → 'A; B; C; D 等 5 人'。"""
    if not authors:
        return "-"
    parts = [a.strip() for a in str(authors).split(";") if a.strip()]
    if len(parts) <= limit:
        return "; ".join(parts)
    return "; ".join(parts[:limit]) + f" 等 {len(parts)} 人"


def _fmt_source(sources) -> str:
    """来源列表 → 可读名称。['ieee', 'scopus'] → 'IEEE, Scopus'。"""
    if not sources:
        return "?"
    if isinstance(sources, str):
        sources = [sources]
    return ", ".join(DB_NAMES.get(s, s) for s in sources)


def main():
    p = argparse.ArgumentParser(description="合并去重后阶段性清单")
    p.add_argument("-i", "--input", required=True, help="merged_filtered.json")
    p.add_argument("-o", "--output", required=True, help="输出的 markdown 文件")
    args = p.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 兼容两种结构：{"papers": [...]} 或直接数组
    papers = data.get("papers", data) if isinstance(data, dict) else data
    if isinstance(papers, dict):
        papers = papers.get("papers", [])
    total = len(papers)

    # 来源统计：按 _source_db 计数（一篇多来源时各来源计一次）
    src_counter: Counter = Counter()
    for paper in papers:
        sources = paper.get("_source_db", [])
        if isinstance(sources, str):
            sources = [sources]
        if sources:
            for s in sources:
                src_counter[s] += 1
        else:
            src_counter["?"] += 1

    lines = []
    lines.append("# 合并去重后论文清单（阶段性）")
    lines.append("")
    lines.append(f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 总篇数: **{total}**（合并去重 + 标题初筛后）")
    if isinstance(data, dict) and data.get("_filtered"):
        flt = data["_filtered"]
        lines.append(f"- 标题初筛: {flt.get('original_total', '?')} → {flt.get('kept', '?')}"
                     f"（移除 {flt.get('removed', '?')} 篇）")
    lines.append("")
    lines.append("## 来源统计")
    lines.append("")
    lines.append("| 来源 | 篇数 |")
    lines.append("|------|------|")
    for s, c in src_counter.most_common():
        lines.append(f"| {DB_NAMES.get(s, s)} | {c} |")
    lines.append(f"| **合计** | **{sum(src_counter.values())}**（一篇多来源时按来源分别计数） |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 论文清单")
    lines.append("")

    for i, paper in enumerate(papers, 1):
        title = (paper.get("title") or "(无标题)").strip()
        lines.append(f"### {i}. {title}")
        lines.append(f"- 作者: {_fmt_authors(paper.get('authors', ''))}")
        meta = f"- 年份: {paper.get('year', '-')} | 来源: {_fmt_source(paper.get('_source_db', []))}"
        if paper.get("venue"):
            meta += f" | 期刊/会议: {paper['venue']}"
        lines.append(meta)
        if paper.get("doi"):
            lines.append(f"- DOI: {paper['doi']}")
        if paper.get("link"):
            lines.append(f"- 链接: {paper['link']}")
        lines.append("")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[MergedList] {total} 篇 → {out}")


if __name__ == "__main__":
    main()
