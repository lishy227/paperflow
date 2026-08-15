#!/usr/bin/env python3



"""



Cross-database result merger: deduplication only.







Scoring is now handled exclusively by paper_ranker.py.



This script only does: load → deduplicate → write.







Usage:



    python scripts/merge_results.py --files ieee.json scopus.json -o merged.json



    python scripts/merge_results.py --files ieee.json -o merged.json    # single DB (N=1)



    echo '<papers_json>' | python scripts/merge_results.py -o merged.json   # stdin JSON







Input format (per database):



    {



      "database": "ieee",        # or scopus / engineering_village / acm / wos



      "totalResults": "59",



      "papers": [



        {



          "title": "...",



          "authors": "Author1; Author2",



          "year": "2024",



          "venue": "IEEE Trans. on ...",



          "type": "Conference Paper",



          "link": "https://...",



          "doi": "10.1109/...",



          "abstract": "...",



          "citations": 42



        }



      ]



    }







Deduplication strategy:

    1. Exact DOI match (case-insensitive, normalize prefix/suffix)

    2. Exact arXiv ID match (无 DOI 论文，arxiv ↔ openalex 预印本跨源去重)

    3. 主键对不上的论文一律不去重；标题高度相似（几乎一样）的论文对

       只进"疑似重复"报告（只读，不自动合并）——同主题论文标题相似很常见，

       自动合并会制造杂交数据（历史教训：包含度相似度把两篇不同综述合并成

       "标题来自 A、DOI 来自 B"的假记录）



For merged papers, keep richest metadata (longest abstract, most fields)
"""









import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))



import argparse



import json



import re



import sys



from pathlib import Path







from utils.doi_utils import normalize as normalize_doi



from utils.pipeline_schema import validate, report, stamp, check_version, PIPELINE_VERSION







# 渠道名规范化映射：CLI/搜索层缩写 → 管道内部全名



# 搜索脚本 --db 传缩写（ev），但 enrich 注册表/配置用全名（engineering_village）



_NORMALIZE_DB = {



    "ev": "engineering_village",



}







import sys, os









# Fix Windows console encoding



if sys.platform == "win32":



    sys.stdout.reconfigure(encoding="utf-8", errors="replace")











# -- Title normalization ----------------------------------------------------







def normalize_title(title):



    """Normalize title for fuzzy comparison."""



    if not title:



        return ""



    t = title.lower()



    t = re.sub(r'[^\w\s]', ' ', t)  # remove punctuation -> spaces



    t = re.sub(r'\s+', ' ', t).strip()



    return t







def title_similarity(t1, t2):

    """Token overlap ratio between two normalized titles.



    仅供"疑似重复"报告使用（不参与自动合并）。

    """

    tokens1 = set(normalize_title(t1).split())

    tokens2 = set(normalize_title(t2).split())

    if not tokens1 or not tokens2:

        return 0.0

    overlap = tokens1 & tokens2

    return len(overlap) / min(len(tokens1), len(tokens2))





def title_jaccard(t1, t2):

    """Jaccard 相似度（overlap / union）。



    报告阈值用 Jaccard 而非包含度：短标题完全被长标题包含时

    包含度得 1.0（误报），Jaccard 不会。

    """

    tokens1 = set(normalize_title(t1).split())

    tokens2 = set(normalize_title(t2).split())

    if not tokens1 or not tokens2:

        return 0.0

    overlap = tokens1 & tokens2

    union = tokens1 | tokens2

    return len(overlap) / len(union)





def normalize_arxiv_id(value) -> str | None:

    """arxiv_id 归一化：小写 + 去版本后缀（2304.06632v2 → 2304.06632）。"""

    if not value:

        return None

    v = str(value).strip().lower()

    v = re.sub(r"v\d+$", "", v)

    return v or None







def merge_duplicates(papers_a, papers_b):



    """



    When two papers are duplicates, merge metadata: keep the richer record.



    """



    # Start with b as base (second DB usually has more metadata)



    merged = dict(papers_b)







    # For each field, keep the longer/more complete version



    for field in ["abstract", "title", "authors", "venue", "type"]:



        val_a = papers_a.get(field, "")



        val_b = papers_b.get(field, "")



        if len(val_a) > len(val_b):



            merged[field] = val_a







    # Keep the non-null DOI



    if not merged.get("doi") and papers_a.get("doi"):



        merged["doi"] = papers_a["doi"]







    # Take max citations (conservatively - the DB seeing it might be broader)



    try:



        c_a = int(papers_a.get("citations") or 0)



        c_b = int(papers_b.get("citations") or 0)



        merged["citations"] = max(c_a, c_b)



    except (ValueError, TypeError):



        merged["citations"] = papers_a.get("citations") or papers_b.get("citations") or 0







    # Track source databases



    sources = set()



    for paper in [papers_a, papers_b]:



        src = paper.get("_source_db", [])



        if isinstance(src, list):



            sources.update(src)



        else:



            sources.add(src)



    merged["_source_db"] = sorted(sources)







    return merged











def find_suspected_duplicates(papers, threshold: float = 0.90) -> list:

    """主键（DOI/arxiv_id）对不上但标题几乎相同的论文对（只读报告）。



    阈值严格（Jaccard ≥ 0.90）：同主题论文标题相似是常态，只有

    "几乎一模一样"才提示，避免噪音。返回 [{title_a, title_b, doi_a,

    doi_b, arxiv_id_a, arxiv_id_b, similarity, sources_a, sources_b}, ...]

    """

    pairs = []

    n = len(papers)

    for i in range(n):

        for j in range(i + 1, n):

            a, b = papers[i], papers[j]

            sim = title_jaccard(a.get("title", ""), b.get("title", ""))

            if sim >= threshold:

                pairs.append({

                    "title_a": (a.get("title") or "")[:120],

                    "title_b": (b.get("title") or "")[:120],

                    "doi_a": a.get("doi") or "",

                    "doi_b": b.get("doi") or "",

                    "arxiv_id_a": a.get("arxiv_id") or "",

                    "arxiv_id_b": b.get("arxiv_id") or "",

                    "similarity": round(sim, 3),

                    "sources_a": a.get("_source_db", []),

                    "sources_b": b.get("_source_db", []),

                })

    return pairs





def merge_all(all_papers_by_db) -> dict:

    """

    Merge papers from multiple databases.



    去重只走主键精确匹配：DOI → arxiv_id。主键对不上不合并；

    标题高度相似的论文对进"疑似重复"报告（只读）。



    Input: list of {database: "ieee", papers: [...]}

    Returns: {"papers": [...], "suspected_duplicates": [...]}

    """

    # Flatten with source DB tags

    all_papers = []

    for db_result in all_papers_by_db:

        db_name = db_result.get("database", "unknown")

        # 渠道名规范化：CLI 层用缩写（ev），管道内部统一用全名（engineering_village）

        # 否则 enrich 的 ENRICHERS 注册表按全名匹配会 miss（历史 bug：EV 从未走 enricher）

        db_name = _NORMALIZE_DB.get(db_name, db_name)

        for paper in db_result.get("papers", []):

            paper["_source_db"] = [db_name]

            all_papers.append(paper)



    # Phase 1: Exact DOI dedup

    doi_index = {}

    no_doi = []

    for paper in all_papers:

        doi = normalize_doi(paper.get("doi"))

        if doi and doi in doi_index:

            doi_index[doi] = merge_duplicates(doi_index[doi], paper)

        elif doi:

            doi_index[doi] = paper

        else:

            no_doi.append(paper)

    merged = list(doi_index.values()) + no_doi



    # Phase 2: Exact arXiv ID dedup（无 DOI 论文跨源去重：arxiv ↔ openalex 预印本）

    # openalex 侧预印本有 DOI（10.48550/arxiv.xxx → arxiv_id 反推），已进 doi_index；

    # 这里对全部论文按 arxiv_id 再合并一次，两边自然对齐。

    arxiv_index = {}

    no_key = []

    for paper in merged:

        aid = normalize_arxiv_id(paper.get("arxiv_id"))

        if aid and aid in arxiv_index:

            arxiv_index[aid] = merge_duplicates(arxiv_index[aid], paper)

        elif aid:

            arxiv_index[aid] = paper

        else:

            no_key.append(paper)

    merged = list(arxiv_index.values()) + no_key



    # Phase 3: 疑似重复报告（只读，不自动合并）

    suspected = find_suspected_duplicates(merged)



    return {"papers": merged, "suspected_duplicates": suspected}





def main():



    parser = argparse.ArgumentParser(



        description="Merge & deduplicate papers from multiple databases"



    )



    parser.add_argument(



        "--files", nargs="+",



        help="JSON files to merge (one per database)"



    )



    parser.add_argument(



        "--output", "-o", required=True,



        help="Output file (required — prevents paper data entering AI context)"



    )



    args = parser.parse_args()







    # Load papers



    all_by_db = []







    if args.files:



        for fpath in args.files:



            try:



                with open(fpath, "r", encoding="utf-8") as f:



                    data = json.load(f)



                if isinstance(data, list):



                    # list of paper dicts (single DB)



                    all_by_db.append({"database": Path(fpath).stem, "papers": data})



                elif isinstance(data, dict):



                    # full DB result



                    all_by_db.append(data)



            except Exception as e:



                print(f"Warning: failed to read {fpath}: {e}", file=sys.stderr)



    else:



        # Read from stdin



        raw = sys.stdin.read().strip()



        if raw:



            try:



                data = json.loads(raw)



                if isinstance(data, list):



                    # List of DB results, e.g. [{"database": "ieee", "papers": [...]}, ...]



                    if data and isinstance(data[0], dict) and "papers" in data[0]:



                        all_by_db = data



                    else:



                        # Single DB's paper list



                        all_by_db = [{"database": "stdin", "papers": data}]



                elif isinstance(data, dict) and "papers" in data:



                    all_by_db = [data]



                else:



                    all_by_db = [{"database": "stdin", "papers": data}]



            except json.JSONDecodeError as e:



                print(f"Error: invalid JSON from stdin: {e}", file=sys.stderr)



                sys.exit(1)







    if not all_by_db:



        print("No input provided. Use --files or pipe JSON to stdin.", file=sys.stderr)



        sys.exit(1)







    # Merge & deduplicate



    result = merge_all(all_by_db)
    merged = result["papers"]
    suspected = result["suspected_duplicates"]







    # 🔧 类型归一化：跨库统一 paper type 标签



    TYPE_MAP = {



        # → Journal Article



        "research-article": "Journal Article", "journal article": "Journal Article",



        "journal_article": "Journal Article", "journals": "Journal Article",



        "article": "Journal Article", "journal": "Journal Article",



        "early access": "Journal Article",



        # → Conference Paper



        "conference paper": "Conference Paper", "conferencepaper": "Conference Paper",



        "conference_article": "Conference Paper", "conference article": "Conference Paper",



        "conference": "Conference Paper", "proceedings": "Conference Paper",



        "proceeding": "Conference Paper", "inproceedings": "Conference Paper",



        "poster": "Conference Paper", "abstract": "Conference Paper",



        "panel": "Conference Paper", "invited-talk": "Conference Paper",



        # → Review



        "review": "Review", "review-article": "Review", "review_article": "Review",



        "surveys": "Review",



        # → Short Paper



        "short paper": "Short Paper", "short-paper": "Short Paper", "shortpaper": "Short Paper",



        # → Preprint



        "preprint": "Preprint",



        # → Book Chapter



        "book chapter": "Book Chapter", "book-chapter": "Book Chapter", "bookchapter": "Book Chapter",



        # → Magazine Article



        "magazine": "Magazine Article", "magazine article": "Magazine Article",



    }



    for paper in merged:



        raw_type = (paper.get("type") or "").strip().lower()



        if raw_type in TYPE_MAP:



            paper["type"] = TYPE_MAP[raw_type]



        elif raw_type and raw_type != "unknown" and len(raw_type) < 30:



            # Already a clean type (e.g. "Journal Article" from IEEE), keep as-is



            paper["type"] = raw_type.title().replace("_", " ")







    # --- 校验 + 报告 ---



    ok, issues = validate(merged, stage="merged")



    if issues:



        report(issues, stage="merged")







    # Output



    output = {



        "total": len(merged),



        "databases": list(set(



            db.get("database", "unknown")



            for db in all_by_db



        )),



        "deduplicated_from": sum(len(db.get("papers", [])) for db in all_by_db),



        "papers": merged,
        "suspected_duplicates": suspected,



    }



    stamp(output, stage="merged")







    json_str = json.dumps(output, ensure_ascii=False, indent=2)







    Path(args.output).parent.mkdir(parents=True, exist_ok=True)



    with open(args.output, "w", encoding="utf-8") as f:



        f.write(json_str + "\n")



    print(f"[Merge] {len(merged)} papers merged (from {len(all_by_db)} databases) → {args.output}")
    if suspected:
        print(f"\n  ⚠ 疑似重复（主键不同，仅提醒，未合并）: {len(suspected)} 对")
        for s in suspected[:10]:
            print(f"    [{s['similarity']:.0%}] {s['title_a'][:60]}  ↔  {s['title_b'][:60]}")
        if len(suspected) > 10:
            print(f"    ... 其余 {len(suspected) - 10} 对见输出文件 suspected_duplicates 字段")











if __name__ == "__main__":



    main()



