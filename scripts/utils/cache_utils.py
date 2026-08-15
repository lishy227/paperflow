#!/usr/bin/env python3
"""
cache_utils.py — paper-abstracts/ 缓存目录的统一操作入口
=========================================================
全项目缓存文件的唯一事实来源。任何脚本涉及缓存文件名生成 / 查找 /
写入 / 读取，**必须** import 本模块，禁止自行拼文件名或直接 open
缓存目录（历史教训：Tier 2 自己发明了 `{db}_doi_` 命名，导致缓存
永远查不到，形同虚设）。

缓存文件名规则（三层，按优先级）：
  1. 有 DOI          → `{normalized_doi}.txt`          （跨渠道共享）
      例: 10.1002_adma.202407791.txt
  2. 无 DOI 有主键   → `{db}_{key_type}_{key}.txt`      （渠道自缓存）
      例: ieee_doc_12345.txt / engineering_village_doc_cpx_xxx.txt
           arxiv_2301.12345.txt / openalex_w2761234567.txt
  3. 都没有          → `{title_md5_12}.txt`            （标题哈希兜底）
      例: 06ca8e2f7543.txt

缓存文件内容统一模板：
    Title: ...
    Authors: ...
    Year: ...
    Venue: ...
    DOI: ...
    Citations: ...

    Abstract:
    <abstract 正文>

旧格式（历史遗留）：
  - `{db}_doi_{doi}.txt`        → 迁移为 `{doi}.txt`
  - `unknown_doc_{key}.txt`     → 按 key 前缀识别渠道，迁移为 `{db}_doc_{key}.txt`
  - 纯大写标题文件名            → 读内容解析 title/DOI 后迁移
"""

import hashlib
import os
import re

from .doi_utils import (
    normalize as _norm_doi,
    extract as _extract_doi,
    to_filename as _doi_to_filename,
    title_hash as _title_hash,
    doc_id_filename as _doc_id_filename,
    extract_doc_id as _extract_doc_id,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 多来源论文的渠道优先级（无 DOI 时按此顺序取主键归属渠道）
DB_PRIORITY = ["ieee", "scopus", "acm", "engineering_village", "openalex", "arxiv"]

# 统一内容模板（所有写缓存的地方必须用 write_cache，禁止自定义格式）
_TEMPLATE = (
    "Title: {title}\n"
    "Authors: {authors}\n"
    "Year: {year}\n"
    "Venue: {venue}\n"
    "DOI: {doi}\n"
    "Citations: {citations}\n"
    "\n"
    "Abstract:\n{abstract}"
)

# ---------------------------------------------------------------------------
# 渠道选择 / 主键提取
# ---------------------------------------------------------------------------

def pick_db(source_dbs) -> str:
    """
    按 DB_PRIORITY 从论文来源渠道列表选一个归属渠道。
    多来源论文（如 [ieee, scopus]）无 DOI 时，主键缓存挂靠优先级最高的渠道。
    """
    if not source_dbs:
        return "unknown"
    if not isinstance(source_dbs, list):
        source_dbs = [source_dbs]
    for db in DB_PRIORITY:
        if db in source_dbs:
            return db
    # 优先级列表之外的渠道（如未来新增 wos）→ 用第一个来源
    return source_dbs[0]


def extract_primary_key(paper: dict) -> tuple[str, str] | None:
    """
    提取渠道主键。返回 (key_type, key) 或 None。

    主键类型（按优先级）：
      - ("doc", ...)      — 扩展数据库 docId（IEEE / EV 等无 DOI 论文）
      - ("arxiv", ...)    — arXiv ID（如 2301.12345，无 DOI 预印本兜底）
      - ("openalex", ...) — OpenAlex ID（如 W2761234567，无 DOI 兜底）
    """
    doc_id = _extract_doc_id(paper)
    if doc_id:
        return ("doc", doc_id)
    arxiv_id = (paper.get("arxiv_id") or "").strip()
    if arxiv_id:
        return ("arxiv", arxiv_id)
    oa_id = (paper.get("openalex_id") or "").strip()
    if oa_id:
        return ("openalex", oa_id)
    return None


def _guess_db_from_key(key: str) -> str:
    """根据主键特征猜渠道：cpx_ → engineering_village，纯数字 → ieee，
    XXXX.NNNNN → arxiv，W数字 → openalex，否则 unknown。"""
    if key.startswith("cpx"):
        return "engineering_village"
    if re.fullmatch(r"\d+", key):
        return "ieee"
    if re.match(r"^\d{4}\.\d+", key):
        return "arxiv"
    if re.match(r"^W\d+$", key):
        return "openalex"
    return "unknown"


# ---------------------------------------------------------------------------
# 文件名生成（唯一入口）
# ---------------------------------------------------------------------------

def cache_filename(paper: dict) -> str:
    """
    按三层规则生成缓存文件名：DOI → (渠道, 主键) → 标题哈希。

    >>> cache_filename({"doi": "10.1002/adma.202407791"})
    '10.1002_adma.202407791.txt'
    >>> cache_filename({"docId": "12345", "_source_db": ["ieee"]})
    'ieee_doc_12345.txt'
    >>> cache_filename({"title": "Test Paper"})
    '1afd1097ef6b.txt'
    """
    # 1. DOI 优先（跨渠道共享）
    doi = _extract_doi(paper)
    if doi:
        return _doi_to_filename(doi)

    # 2. 渠道主键（渠道自缓存）
    key = extract_primary_key(paper)
    if key:
        key_type, key_val = key
        if key_type == "doc":
            # 渠道名与主键一致：先按主键特征猜渠道，猜不出再用来源优先级
            db = _guess_db_from_key(key_val)
            if db == "unknown":
                db = pick_db(paper.get("_source_db", []))
            return _doc_id_filename(key_val, db)
        # arxiv / openalex：主键类型即渠道名，直接 {type}_{key}.txt
        return f"{key_type}_{key_val}.txt"

    # 3. 标题哈希兜底
    return _title_hash(paper.get("title", "") or "unknown")


# ---------------------------------------------------------------------------
# 查找
# ---------------------------------------------------------------------------

def find_cache(paper: dict, abstracts_dir: str = "memory/paper-abstracts") -> str | None:
    """
    查找缓存文件。返回路径或 None。

    验证策略（只按新格式单一文件名查找，旧格式兼容已随迁移移除）：
      - DOI 命中 → 直接信（DOI 全局唯一，无需标题验证）
      - docId 命中 → 直接信（渠道主键唯一）
      - 标题哈希命中 → 读第一行标题做 token overlap 验证（防内容污染）
    """
    if not os.path.isdir(abstracts_dir):
        return None

    name = cache_filename(paper)
    path = os.path.join(abstracts_dir, name)
    if not os.path.exists(path):
        return None

    doi = _extract_doi(paper)
    key = extract_primary_key(paper)
    if doi and name.startswith("10."):
        return path
    if key:
        kt, kv = key
        if kt == "doc" and "_doc_" in name:
            return path
        if kt in ("arxiv", "openalex") and name.startswith(kt + "_"):
            return path
    if _verify_title_against_file(paper.get("title", ""), path):
        return path
    return None


def _verify_title_against_file(title: str, path: str) -> bool:
    """读缓存文件第一行 Title，token overlap ≥ 0.5 判定匹配。"""
    if not title:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            first = f.readline().strip()
    except (IOError, UnicodeDecodeError):
        return False
    cached = first[len("Title:"):].strip() if first.startswith("Title:") else first
    if not cached:
        return False
    t1 = set(re.sub(r"[^\w\s]", " ", title.lower()).split())
    t2 = set(re.sub(r"[^\w\s]", " ", cached.lower()).split())
    if not t1 or not t2:
        return False
    return len(t1 & t2) / min(len(t1), len(t2)) >= 0.5


# ---------------------------------------------------------------------------
# 写入 / 读取（唯一入口）
# ---------------------------------------------------------------------------

def write_cache(paper: dict, abstract: str,
                abstracts_dir: str = "memory/paper-abstracts",
                extra: dict | None = None) -> str | None:
    """
    按统一模板写缓存文件。返回文件名，abstract 过短（<50）时返回 None。
    """
    abstract = (abstract or "").strip()
    if len(abstract) < 50:
        return None

    os.makedirs(abstracts_dir, exist_ok=True)
    filename = cache_filename(paper)
    path = os.path.join(abstracts_dir, filename)

    content = _TEMPLATE.format(
        title=(paper.get("title") or "").strip(),
        authors=(extra or {}).get("authors", paper.get("authors", "")),
        year=(extra or {}).get("year", paper.get("year", "")),
        venue=(extra or {}).get("venue", paper.get("venue", "")),
        doi=_extract_doi(paper) or "",
        citations=(extra or {}).get("citations", paper.get("citations", "")),
        abstract=abstract,
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return filename


def read_abstract(path: str) -> str:
    """
    从缓存文件读摘要正文。兼容统一模板（Abstract: 标记）与旧格式（全文即摘要）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, UnicodeDecodeError):
        return ""
    idx = content.find("\nAbstract:\n")
    if idx >= 0:
        return content[idx + len("\nAbstract:\n"):].strip()
    return content.strip()


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------

def _is_new_format(name: str) -> bool:
    """判断文件名是否已符合新规则（doi / {db}_doc_ / title_hash）。"""
    if re.match(r"^10\.\d+.*\.txt$", name):      # DOI 无前缀
        return True
    if re.match(r"^(ieee|scopus|acm|engineering_village)_doc_[^/]+\.txt$", name):
        return True
    if re.match(r"^(arxiv|openalex)_[^/]+\.txt$", name):
        return True
    if re.match(r"^[0-9a-f]{12}\.txt$", name):   # 标题哈希
        return True
    return False


def _parse_content(content: str) -> tuple[str, str]:
    """从缓存文件内容解析 (title, doi)。兼容统一模板与旧格式。"""
    title, doi = "", ""
    for line in content.splitlines():
        if line.startswith("Title:") and not title:
            title = line[6:].strip()
        elif line.startswith("DOI:") and not doi:
            doi = line[4:].strip()
        elif line.startswith("10.") and not doi:  # 旧格式可能直接以 DOI 开头
            m = re.match(r"(10\.\S+)", line)
            if m:
                doi = m.group(1)
        if title and doi:
            break
    return title, doi


def _target_name_from_old_name(name: str, content: str) -> str | None:
    """
    旧文件名 → 新文件名。返回 None 表示无法转换（应删除）。
    优先用文件名里的结构（{db}_doi_ / {db}_doc_ / unknown_doc_），内容解析兜底。
    """
    title, doi = _parse_content(content)

    # 1. {db}_doi_{doi}.txt → {doi}.txt（保持原大小写，与现有缓存一致）
    m = re.match(r"^(?:ieee|scopus|acm|engineering_village|ev|unknown)_doi_(.+?)\.txt$", name)
    if m:
        raw_doi = m.group(1).replace("_", "/")
        # 验证是合法 DOI（10. 前缀）但不强制小写化
        if raw_doi.startswith("10.") and len(raw_doi) > 10:
            return _doi_to_filename(raw_doi)
        return None

    # 2. {db}_doc_{key}.txt / unknown_doc_{key}.txt → 按 key 特征定渠道
    m = re.match(r"^(?:ieee|scopus|acm|engineering_village|ev|unknown)_doc_(.+?)\.txt$", name)
    if m:
        key = m.group(1)
        db = _guess_db_from_key(key)
        if db != "unknown":
            return _doc_id_filename(key, db)
        return None

    # 3. 纯数字 docid（旧格式）→ ieee_doc
    if re.fullmatch(r"\d+\.txt", name):
        return _doc_id_filename(name[:-4], "ieee")

    # 4. 大写标题文件名 → 有 DOI 转 DOI，否则标题哈希
    if doi:
        norm = _norm_doi(doi)
        if norm:
            return _doi_to_filename(norm)
    if title:
        return _title_hash(title)
    return None


def migrate_legacy(abstracts_dir: str = "memory/paper-abstracts",
                   dry_run: bool = False) -> dict:
    """
    旧格式 → 新格式迁移。

    转换规则：
      - `{db}_doi_{doi}.txt`     → `{doi}.txt`
      - `{db}_doc_{key}.txt`     → `{db}_doc_{key}.txt`（渠道名规范化）
      - `unknown_doc_{key}.txt`  → 按 key 前缀识别渠道（cpx_ → ev），
                                   无法识别且无 DOI → 删除
      - 大写标题文件名           → 读内容解析 title/DOI 后按新规则重写
      - 已符合新规则的 → 不动

    返回统计: {"converted": [...], "deleted": [...], "kept": int, "failed": [...]}
    """
    if not os.path.isdir(abstracts_dir):
        return {"converted": [], "deleted": [], "kept": 0, "failed": []}

    converted, deleted, failed = [], [], []
    kept = 0

    for name in sorted(os.listdir(abstracts_dir)):
        if not name.endswith(".txt"):
            continue
        path = os.path.join(abstracts_dir, name)

        # 已经是新格式？
        if _is_new_format(name):
            kept += 1
            continue

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (IOError, UnicodeDecodeError):
            deleted.append(name)
            continue

        new_name = _target_name_from_old_name(name, content)
        if not new_name:
            deleted.append(name)
            if not dry_run:
                os.remove(path)
            continue

        if new_name == name:
            kept += 1
            continue

        # 目标已存在（内容更长者胜）
        target = os.path.join(abstracts_dir, new_name)
        if os.path.exists(target):
            try:
                old_len = os.path.getsize(target)
            except OSError:
                old_len = 0
            if len(content) <= old_len:
                deleted.append(name)   # 旧文件更短 → 去重删除
                if not dry_run:
                    os.remove(path)
                continue
            # 新内容更长 → 覆盖（os.replace 原子替换，Windows 安全）
        converted.append(f"{name} → {new_name}")
        if not dry_run:
            os.replace(path, target)

    return {"converted": converted, "deleted": deleted, "kept": kept, "failed": failed}


if __name__ == "__main__":
    import sys
    if "--migrate" in sys.argv:
        dry = "--dry-run" in sys.argv
        stats = migrate_legacy(dry_run=dry)
        print(f"[migrate] converted={len(stats['converted'])} "
              f"deleted={len(stats['deleted'])} kept={stats['kept']} "
              f"failed={len(stats['failed'])}")
        for c in stats["converted"][:20]:
            print(f"  ✓ {c}")
        for d in stats["deleted"][:20]:
            print(f"  ✗ 删除 {d}")
        print(f"[migrate] {'（dry-run，未实际改动）' if dry else '完成'}")
    else:
        import doctest
        doctest.testmod(verbose=False)
        print("OK: cache_utils doctest 通过")
