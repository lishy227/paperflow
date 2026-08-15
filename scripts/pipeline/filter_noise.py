#!/usr/bin/env python3
"""
filter_noise.py — 标题初筛，砍掉明显不相关的论文
====================================================

管道位置：Stage 2 (merge) → filter_noise.py → Stage 3 (enrich)

策略（按优先级）：
  1. 标题格式异常 — 太短/纯符号/纯数字/无意义短语
  2. 标题关键词零命中 — 标题中找不到任何搜索概念的 token
  3. 来源库质量加权 — EV 返回的杂音多，标题命中阈值更高

用法：
  python filter_noise.py -i merged_results.json -c search_concept.json -o merged_filtered.json
"""

import argparse, json, re, sys, os
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.pipeline_schema import validate, report

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

STOPWORDS_2CHAR = {
    "of", "to", "in", "is", "it", "on", "at", "be", "by", "or",
    "an", "as", "no", "so", "we", "he", "do", "if", "go", "my",
    "me", "us", "up", "am", "oh", "ha", "hi", "ok", "vs",
}


def tokenize(text: str) -> set[str]:
    """提取有意义的词（>=2 字符），支持 2 字符关键词如 AI, ML。"""
    if not text:
        return set()
    t = re.sub(r'[^\w\s]', ' ', str(text).lower())
    return {w for w in t.split()
            if len(w) >= 2 and not (len(w) == 2 and w in STOPWORDS_2CHAR)}

def extract_keyword_tokens(concept: dict) -> tuple[set[str], set[str]]:
    """
    从 search_concept.json 提取两种 token 集合。
    Returns: (core_tokens, exclude_tokens)
    """
    core = set()
    for c in concept.get("core_concepts", []):
        core.update(tokenize(c))
    for st in concept.get("sub_topics", []):
        core.update(tokenize(st))

    # Synonyms: 每个 term 也贡献 token
    for term, syns in concept.get("synonyms", {}).items():
        core.update(tokenize(term))
        for s in syns:
            core.update(tokenize(s))

    exclude = set()
    for ex in concept.get("exclude", []):
        exclude.update(tokenize(ex))

    return core, exclude


# ---------------------------------------------------------------------------
# Title quality checks
# ---------------------------------------------------------------------------

# 无意义的通用标题模式（这些标题几乎不可能是相关学术论文）
JUNK_TITLE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'^untitled$',
        r'^no title$',
        r'^abstract$',
        r'^introduction$',
        r'^conclusion$',
        r'^references?$',
        r'^acknowledgements?$',
        r'^table of contents$',
        r'^index$',
        r'^preface$',
        r'^foreword$',
        r'^editorial$',
        r'^corrigendum$',
        r'^erratum$',
        r'^retraction$',
        r'^cover\s+(page|image|photo)$',
        r'^announcement$',
        r'^call\s+for\s+papers?$',
        r'^guest\s+editorial$',
        r'^\d{4}\s+index$',
    ]
]

# 通用/占位标题模式（模糊匹配）
SUSPICIOUS_TITLE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'^\W+$',                        # 纯符号
        r'^\d+$',                        # 纯数字
        r'^[a-z]\d{2,}$',               # 单字母+数字（如 P12345）
        r'^special\s+issue',             # 特刊占位
        r'^front\s+matter',              # 期刊前页
        r'^back\s+matter',               # 期刊后页
    ]
]


def check_title_quality(title: str) -> Optional[str]:
    """
    检查标题质量。返回 None = 通过；返回字符串 = 拒绝原因。
    """
    if not title:
        return "empty_title"

    title_clean = title.strip()

    # 太短（学术论文标题一般 > 30 字符）
    if len(title_clean) < 20:
        return f"title_too_short({len(title_clean)} chars)"

    # 太长也可能有问题（> 500 字符可能是错误数据）
    if len(title_clean) > 500:
        return f"title_too_long({len(title_clean)} chars)"

    # 精确匹配垃圾标题
    for pat in JUNK_TITLE_PATTERNS:
        if pat.fullmatch(title_clean):
            return f"junk_title:{pat.pattern}"

    # 模糊匹配可疑标题
    for pat in SUSPICIOUS_TITLE_PATTERNS:
        if pat.search(title_clean):
            return f"suspicious_title:{pat.pattern}"

    # 标题中英文单词太少（< 3 个有意义词）
    words = [w for w in title_clean.lower().split()
             if len(w) > 2 and w.isalpha()]
    if len(words) < 3:
        return f"too_few_words({len(words)})"

    return None


# ---------------------------------------------------------------------------
# Title-keyword relevance
# ---------------------------------------------------------------------------

def title_keyword_score(title: str, core_tokens: set[str],
                        exclude_tokens: set[str]) -> float:
    """
    计算标题与搜索主题的匹配度。返回 0.0-1.0。

    策略：标题 token 中有多大比例命中了核心概念？
    这样即使 core_concepts 很多（token 集合大），只要标题里有一定比例的相关词就能通过。
    """
    if not core_tokens:
        return 0.0

    title_tokens = tokenize(title)
    if not title_tokens:
        return 0.0

    # 主分数：标题 token 中命中核心概念的比例
    hits = len(title_tokens & core_tokens)
    score = hits / len(title_tokens)

    # 排除词命中 → 惩罚
    if exclude_tokens:
        exclude_hits = len(title_tokens & exclude_tokens)
        if exclude_hits > 0:
            score -= (exclude_hits / len(title_tokens)) * 0.5

    return max(0.0, score)


# ---------------------------------------------------------------------------
# Source-DB quality adjustment
# ---------------------------------------------------------------------------

# 各库的标题命中阈值（EV 杂音多，门槛更高）
# 阈值含义：标题中有多少比例的词能命中搜索概念
DB_MIN_TITLE_SCORE = {
    "engineering_village": 0.15,
    "ieee": 0.06,
    "acm": 0.08,
    "scopus": 0.04,
    "default": 0.08,
}


# ---------------------------------------------------------------------------
# Main filter
# ---------------------------------------------------------------------------

def filter_papers(merged_path: str, concept_path: str,
                  output_path: str, min_title_score: float = 0.05) -> dict:
    """
    加载合并结果 → 标题质量检查 + 关键词初筛 → 输出去噪版本。
    Returns: {kept: N, removed: N, removed_details: [...]}
    """
    # Load merged results
    with open(merged_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    papers = data.get("papers", data if isinstance(data, list) else [])

    # Load search concept
    concept = {}
    if concept_path and Path(concept_path).exists():
        with open(concept_path, "r", encoding="utf-8") as f:
            concept = json.load(f)

    core_tokens, exclude_tokens = extract_keyword_tokens(concept)

    total = len(papers)
    kept = []
    removed = []
    removed_details = []

    for paper in papers:
        title = paper.get("title", "")
        sources = paper.get("_source_db", [])

        # 1. Title quality check
        quality_issue = check_title_quality(title)
        if quality_issue:
            removed.append(paper)
            removed_details.append({
                "title": title[:80],
                "source_db": sources,
                "reason": quality_issue,
            })
            continue

        # 2. Title-keyword relevance check
        if core_tokens:
            title_score = title_keyword_score(title, core_tokens, exclude_tokens)

            # 根据来源库确定阈值
            if isinstance(sources, list) and sources:
                db_thresholds = [DB_MIN_TITLE_SCORE.get(s, DB_MIN_TITLE_SCORE["default"])
                                for s in sources]
                threshold = max(db_thresholds)  # 取最严格的
            else:
                threshold = DB_MIN_TITLE_SCORE["default"]

            if title_score < threshold:
                removed.append(paper)
                removed_details.append({
                    "title": title[:80],
                    "source_db": sources,
                    "reason": f"title_score_too_low({title_score:.3f} < {threshold})",
                })
                continue

        # Passed all checks
        kept.append(paper)

    # Update data
    if isinstance(data, dict) and "papers" in data:
        data["papers"] = kept
        data["_filtered"] = {
            "original_total": total,
            "kept": len(kept),
            "removed": len(removed),
            "title_filter_enabled": bool(core_tokens),
        }
        output_data = data
    else:
        output_data = {
            "total": len(kept),
            "papers": kept,
            "_filtered": {
                "original_total": total,
                "kept": len(kept),
                "removed": len(removed),
            },
        }

    # Write output
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # Summary
    pct = (len(removed) / total * 100) if total > 0 else 0
    print(f"[FilterNoise] {total} → {len(kept)} kept, "
          f"{len(removed)} removed ({pct:.1f}%)")

    # Breakdown by reason
    reason_counts = {}
    for d in removed_details:
        reason = d["reason"].split(":")[0] if ":" in d["reason"] else d["reason"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in reason_counts.items():
        print(f"  {reason}: {count}")

    # Show removed titles (first 10)
    if removed_details:
        print(f"\n  Removed papers (first 10):")
        for d in removed_details[:10]:
            src = "+".join(d["source_db"]) if d["source_db"] else "?"
            print(f"    [{src}] {d['title'][:70]} — {d['reason']}")

    return {
        "kept": len(kept),
        "removed": len(removed),
        "removed_details": removed_details,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Title-based noise filter for merged paper results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python filter_noise.py -i merged_results.json -c search_concept.json -o merged_filtered.json
  python filter_noise.py -i merged_results.json -o merged_filtered.json    # no concept, quality only
        """,
    )
    parser.add_argument("-i", "--input", required=True,
                        help="Merged results JSON")
    parser.add_argument("-c", "--concept", default=None,
                        help="search_concept.json (optional, enables keyword filter)")
    parser.add_argument("-o", "--output", required=True,
                        help="Filtered output JSON")
    parser.add_argument("--min-title-score", type=float, default=0.05,
                        help="Minimum title-keyword score (default: 0.05)")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"[ERROR] Input not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    filter_papers(args.input, args.concept, args.output, args.min_title_score)


if __name__ == "__main__":
    main()
