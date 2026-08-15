#!/usr/bin/env python3
"""
main.py — paperflow standalone 管道入口
============================================

替代 AI 编排：状态机调度 + 分级报错 + 人类可读运行报告。

用法：
    python main.py init <run_id>                     # 创建运行文件夹 + search_concept 模板
    python main.py launch                            # 启动浏览器（访问引导）
    python main.py health <run_id>                   # 只跑健康检查
    python main.py run <run_id>                      # 跑全管道
    python main.py run <run_id> --from merge         # 从指定阶段开始
    python main.py run <run_id> --to rank            # 只跑到指定阶段
    python main.py run <run_id> --yes                # 非交互（决策点用默认策略）
    python main.py report <run_id>                   # 查看运行报告

退出码：
    0  成功
    1  可重试失败（页面空 / API 限流 / 会话失效）
    2  环境/配置错误（缺依赖 / 缺 key / 浏览器没启动）

设计原则（替代 AI 的决策职责）：
    - 确定性规则（产物存在性、检查点阈值）→ 代码直接判断
    - 模糊判断（缺失率略超、搜索量偏少）→ 交互问人，--yes 用默认策略
    - 失败信息必须"可行动"：为什么失败 + 用户该做什么
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from utils.env_config import PROJECT_ROOT, load_dotenv, get_scopus_api_key, get_llm_config, get_cdp_base
from utils.encoding import ensure_utf8_stdio

ensure_utf8_stdio()

# ─────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────

PYTHON = sys.executable
RUN_DIR_BASE = PROJECT_ROOT / "memory" / "output"
CACHE_DIR = PROJECT_ROOT / "memory" / "paper-abstracts"

# 各库搜索目标数（检查点阈值）
SEARCH_TARGET = 40
SEARCH_MIN_OK = 30

# 摘要缺失率阈值（check_abstracts.py 内置 5%）
ABSTRACT_MISS_THRESHOLD = 5.0

STAGES = [
    {"id": "health", "name": "健康检查"},
    {"id": "search", "name": "多库搜索"},
    {"id": "merge", "name": "合并去重"},
    {"id": "enrich", "name": "摘要补全"},
    {"id": "rank", "name": "评分排序"},
    {"id": "output", "name": "生成输出"},
]

# 每个阶段对应的文件夹（步骤目录架构：中间量按步归档）
STAGE_DIRS = {
    "health": "step0_health",
    "search": "step1_search",
    "merge": "step2_merge",
    "enrich": "step3_enrich",
    "rank": "step4_rank",
    "output": "step5_output",
}

# 每个阶段应产出的文件（相对 run_dir 的 step 文件夹，用于断点续跑判断）
STAGE_OUTPUTS = {
    "health": [],
    "search": [],  # search 结果文件按任务启用的数据库动态收集（见 search_result_files）
    "merge": ["step2_merge/merged_filtered.json"],
    "enrich": ["step3_enrich/enriched.json"],
    "rank": ["step4_rank/themed.json"],
    "output": ["step5_output/final_results.md", "step5_output/results.bib"],
}

# 数据库 key → 搜索结果文件名（扩展 + 免费）
_DB_RESULT_FILE = {
    "scopus": "scopus_results.json",
    "ieee": "ieee_results.json",
    "acm": "acm_results.json",
    "engineering_village": "ev_results.json",
    "openalex": "openalex_results.json",
}


def _enabled_dbs(run_dir: Path) -> list[str]:
    """任务启用的数据库列表（读 search_concept.json；读不到/无字段 → 全部）。"""
    try:
        concept = json.loads((run_dir / "search_concept.json").read_text(encoding="utf-8"))
        dbs = concept.get("databases") or []
        if dbs:
            return dbs
    except Exception:
        pass
    return list(_DB_RESULT_FILE.keys())


def search_result_files(run_dir: Path) -> list[Path]:
    """任务启用数据库的搜索结果文件（新 step 目录 + 旧平铺兼容）。"""
    files = []
    for db in _enabled_dbs(run_dir):
        name = _DB_RESULT_FILE.get(db)
        if not name:
            continue
        p = run_dir / "step1_search" / name
        if not p.exists():
            p = run_dir / name
        if p.exists():
            files.append(p)
    return files


def stage_path(run_dir: Path, stage: str) -> Path:
    """阶段文件夹路径（自动创建）。"""
    p = run_dir / STAGE_DIRS.get(stage, stage)
    p.mkdir(parents=True, exist_ok=True)
    return p


def stage_file(run_dir: Path, stage: str, filename: str) -> Path:
    """阶段内文件路径（自动创建文件夹）。"""
    return stage_path(run_dir, stage) / filename


# ─────────────────────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────────────────────

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_cmd(cmd: list, run_dir: Path, stage: str, cwd: Path = PROJECT_ROOT) -> tuple[bool, str]:
    """执行子命令，输出进该阶段文件夹的日志。返回 (ok, 摘要行)。"""
    log_path = stage_file(run_dir, stage, "log.txt")
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n===== {now_str()} $ {' '.join(str(c) for c in cmd)} =====\n")
        try:
            proc = subprocess.run(
                cmd, cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=3600,
            )
        except subprocess.TimeoutExpired:
            logf.write("[TIMEOUT] 超过 1 小时\n")
            return False, "执行超时（>1h）"
        except Exception as e:
            logf.write(f"[SPAWN ERROR] {e}\n")
            return False, f"无法启动进程: {e}"
        logf.write(proc.stdout)
        logf.write(proc.stderr)
    # 取最后一行非空输出作为摘要
    lines = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip()]
    summary = lines[-1].strip() if lines else f"exit={proc.returncode}"
    return proc.returncode == 0, summary


def stage_done(run_dir: Path, stage: str) -> bool:
    """阶段是否已完成（产物齐全且有效）。兼容旧版平铺路径。

    health 特殊：无产物，恒返回 False——每次 run 都重新执行健康检查。
    search 特殊：结果文件按任务启用的数据库检查（免费源/扩展数据库各自独立）。
    """
    if stage == "health":
        return False
    if stage == "search":
        files = search_result_files(run_dir)
        if not files:
            return False
        # 所有启用数据库的结果文件都必须存在且 count > 0
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if data.get("count", 0) <= 0:
                    return False
            except Exception:
                return False
        return len(files) == len(_enabled_dbs(run_dir))
    for f in STAGE_OUTPUTS.get(stage, []):
        p = run_dir / f
        if not p.exists():
            # 旧格式兼容：产物平铺在 run_dir 根（如 scopus_results.json）
            legacy = run_dir / Path(f).name
            if not legacy.exists():
                return False
            p = legacy
    return True


def ask(question: str, options: list[tuple[str, str]], default: str, yes: bool) -> str:
    """交互决策点。options: [(key, 描述), ...]。yes=True 时直接选 default。"""
    if yes:
        print(f"  [--yes] {question} → 默认选择: {default}")
        return default
    print(f"\n  ? {question}")
    for key, desc in options:
        mark = " [默认]" if key == default else ""
        print(f"    [{key}] {desc}{mark}")
    while True:
        choice = input(f"  选择 ({'/'.join(k for k, _ in options)}): ").strip().lower()
        if choice in (k for k, _ in options):
            return choice
        if not choice:
            return default
        print("    无效输入，请重新选择")


def write_stage_report(run_dir: Path, stage: str, ok: bool, message: str, detail: dict = None, status: str = "done"):
    """写阶段报告 JSON。status: done=阶段已结束 / running=阶段运行中。"""
    report = {
        "stage": stage,
        "ok": ok,
        "ts": now_str(),
        "message": message,
        "status": status,
        "detail": detail or {},
    }
    with open(run_dir / "stage_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def update_run_report(run_dir: Path, lines: list[str]):
    """追加/重建 run_report.md。"""
    path = run_dir / "run_report.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 运行报告\n\n")
        f.write(f"更新时间: {now_str()}\n\n")
        f.write("## 阶段进度\n\n")
        for line in lines:
            f.write(line + "\n")
    return path


def ensure_run_dir(run_id: str) -> Path:
    """确保运行文件夹存在，返回其路径。"""
    run_dir = RUN_DIR_BASE / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def load_concept(run_dir: Path) -> dict:
    path = run_dir / "search_concept.json"
    if not path.exists():
        print(f"[ERROR] 缺少 {path}。先运行: python main.py init {run_dir.name}", file=sys.stderr)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────
# 阶段实现
# ─────────────────────────────────────────────────────────────

def stage_health(run_dir: Path, yes: bool, force: bool = False) -> tuple[bool, str]:
    """Stage 0: 统一健康检查（依赖 / key / 浏览器 / 会话）。

    按任务选择的数据库按需检查：全部为免费源（openalex）时
    跳过 key/浏览器/会话检查（免费源纯 API 不需要）。
    """
    print(f"\n=== Stage 0 健康检查 ===")
    from health_check import run_all, print_report
    # 任务可能没有 search_concept.json（如 health 命令跑在空 run_dir）→ 全查
    dbs = None
    try:
        concept = load_concept(run_dir)
        dbs = concept.get("databases") or None
    except SystemExit:
        pass
    report = run_all(databases=dbs)
    print_report(report)

    # 报告写进日志
    log_path = stage_file(run_dir, "health", "log.txt")
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n===== {now_str()} health check =====\n")
        logf.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    if report["ok"]:
        return True, "配置 + 浏览器 + 会话全部正常"
    return False, report["summary"]


def stage_search(run_dir: Path, yes: bool, force: bool = False) -> tuple[bool, str]:
    """Stage 1: 多库串行搜索。"""
    print(f"\n=== Stage 1 多库搜索 ===")
    concept = load_concept(run_dir)

    # 生成查询
    sys.path.insert(0, str(PROJECT_ROOT / "scripts/pipeline"))
    from query_builder import build_all, validate_concept
    for w in validate_concept(concept):
        print(f"  ⚠️  {w}")
    queries = build_all(concept)

    target = concept.get("target", SEARCH_TARGET)
    results = {}
    order = ["scopus", "ieee", "acm", "engineering_village"]

    # --- 免费源（OpenAlex，纯 API，无 key 无浏览器） ---
    for db in ["openalex"]:
        info = queries.get(db, {})
        if "error" in info or db not in concept.get("databases", []):
            print(f"  [skip] {db}: 未启用或查询构建失败")
            continue
        out = stage_file(run_dir, "search", f"{db}_results.json")
        if out.exists() and not force:
            try:
                with open(out, encoding="utf-8") as f:
                    existing = json.load(f).get("count", 0)
            except Exception:
                existing = 0
            if existing > 0:
                results[db] = existing
                print(f"  [skip] {db}: 已有产物 ({existing} 篇)，--force 可重搜")
                continue
        print(f"  [{db}] 搜索: {info.get('query', '')[:80]}...")
        ok, summary = run_cmd(
            [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/_search_free.py"),
             "--db", db, "--url", info["query"], "--target", str(target),
             "--year", str(concept.get("year_range", "")),
             "--output", str(out)],
            run_dir, "search",
        )
        if not ok or not out.exists():
            return False, f"{db} 搜索失败: {summary}"
        with open(out, encoding="utf-8") as f:
            results[db] = json.load(f).get("count", 0)
        print(f"    → {results[db]} 篇")

    # --- Scopus（API，无浏览器） ---
    db = "scopus"
    info = queries.get(db, {})
    if "error" in info or db not in concept.get("databases", list(queries.keys())):
        print(f"  [skip] {db}: 未启用或查询构建失败")
    else:
        scopus_out = stage_file(run_dir, "search", "scopus_results.json")
        existing = 0
        if scopus_out.exists() and not force:
            try:
                with open(scopus_out, encoding="utf-8") as f:
                    existing = json.load(f).get("count", 0)
            except Exception:
                existing = 0
            if existing > 0:
                results[db] = existing
                print(f"  [skip] {db}: 已有产物 ({existing} 篇)，--force 可重搜")
        if existing == 0:
            print(f"  [scopus] 搜索: {info.get('query', '')[:80]}...")
            qfile = stage_file(run_dir, "search", "scopus_query.txt")
            qfile.write_text(info["query"], encoding="utf-8")
            ok, summary = run_cmd(
                [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/_search_scopus.py"),
                 "--query-file", str(qfile), "--target", str(target),
                 "--output", str(scopus_out)],
                run_dir, "search",
            )
            if not ok or not scopus_out.exists():
                return False, f"scopus 搜索失败: {summary}"
            with open(scopus_out, encoding="utf-8") as f:
                results[db] = json.load(f).get("count", 0)
            print(f"    → {results[db]} 篇")

    # --- IEEE / ACM / EV（CDP 浏览器，subprocess 调 _search_one.py） ---
    def browser_search(db, url, extractor, paginate, param, first):
        nonlocal results
        out = stage_file(run_dir, "search", f"{db}_results.json")
        if out.exists() and not force:
            try:
                with open(out, encoding="utf-8") as f:
                    existing = json.load(f).get("count", 0)
            except Exception:
                existing = 0
            if existing > 0:
                results[db] = existing
                print(f"    [skip] {db}: 已有产物 ({existing} 篇)，--force 可重搜")
                return True, f"{db}: 复用已有产物 ({existing} 篇)"
        ok, summary = run_cmd(
            [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/_search_one.py"),
             "--db", db, "--url", url, "--extractor", str(PROJECT_ROOT / extractor),
             "--output", str(out), "--target", str(target),
             "--wait", "12", "--paginate", paginate, "--param", param, "--first", str(first)],
            run_dir, "search",
        )
        if not ok or not out.exists():
            return False, f"{db} 搜索失败: {summary}"
        with open(out, encoding="utf-8") as f:
            results[db] = json.load(f).get("count", 0)
        print(f"    → {results[db]} 篇")
        return True, f"{db}: {results[db]} 篇"

    for db in ["ieee", "acm"]:
        info = queries.get(db, {})
        if "error" in info or db not in concept.get("databases", []):
            print(f"  [skip] {db}: 未启用")
            continue
        print(f"  [{db}] 搜索...")
        first = 0 if db == "acm" else 1
        param = "startPage" if db == "acm" else "pageNumber"
        ok, msg = browser_search(db, info["query"], f"extractors/{db}.js",
                                 "url_param", param, first)
        if not ok:
            return False, msg

    db = "engineering_village"
    info = queries.get(db, {})
    if "error" not in info and db in concept.get("databases", []):
        print(f"  [ev] 搜索（表单填写）...")
        ev_url = info.get("query", "")
        # query_builder 返回的是带 searchQuery1 参数的 URL，提取纯查询串
        from urllib.parse import urlparse, parse_qs
        ev_query = parse_qs(urlparse(ev_url).query).get("searchQuery1", [""])[0]
        if not ev_query:
            return False, "ev: 无法从查询 URL 提取 searchQuery1"
        out = stage_file(run_dir, "search", "ev_results.json")
        ok, summary = run_cmd(
            [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/_search_one.py"),
             "--db", "ev", "--url", "https://www.engineeringvillage.com/app/search/quick/",
             "--query", ev_query, "--extractor", str(PROJECT_ROOT / "extractors/engineering_village.js"),
             "--output", str(out), "--target", str(target), "--wait", "12",
             "--paginate", "click"],
            run_dir, "search",
        )
        if not ok or not out.exists():
            return False, f"ev 搜索失败: {summary}"
        with open(out, encoding="utf-8") as f:
            results[db] = json.load(f).get("count", 0)
        print(f"    → {results[db]} 篇")
    else:
        print(f"  [skip] ev: 未启用")

    # 检查点：搜索覆盖
    low = {k: v for k, v in results.items() if v < SEARCH_MIN_OK}
    msg = "搜索完成: " + ", ".join(f"{k}={v}" for k, v in results.items())
    if low:
        low_desc = ", ".join(f"{k}({v}篇)" for k, v in low.items())
        choice = ask(
            f"以下数据库结果偏少（阈值 {SEARCH_MIN_OK}）: {low_desc}",
            [("c", "继续管道（可能查询太窄，结果仍有参考价值）"),
             ("s", "停止，我去调整 search_concept.json 再重跑")],
            "c", yes,
        )
        if choice == "s":
            return False, msg + f"；用户选择停止调整查询"
    return True, msg


def stage_merge(run_dir: Path, yes: bool, force: bool = False) -> tuple[bool, str]:
    """Stage 2: 合并去重 + 标题初筛。"""
    print(f"\n=== Stage 2 合并去重 ===")
    files = [str(p) for p in search_result_files(run_dir)]
    if not files:
        return False, "没有任何搜索结果文件，无法合并"

    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/merge_results.py"),
         "--files", *files, "--output", str(stage_file(run_dir, "merge", "merged_results.json"))],
        run_dir, "merge",
    )
    if not ok:
        return False, f"合并失败: {summary}"

    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/filter_noise.py"),
         "-i", str(stage_file(run_dir, "merge", "merged_results.json")),
         "-c", str(run_dir / "search_concept.json"),
         "-o", str(stage_file(run_dir, "merge", "merged_filtered.json"))],
        run_dir, "merge",
    )
    if not ok:
        return False, f"标题初筛失败: {summary}"

    # 阶段性清单（merge 后、enrich 前）：来源统计 + 逐篇基本信息，供人工查阅/统计
    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/make_merged_list.py"),
         "-i", str(stage_file(run_dir, "merge", "merged_filtered.json")),
         "-o", str(stage_file(run_dir, "merge", "merged_list.md"))],
        run_dir, "merge",
    )
    if not ok:
        return False, f"阶段性清单生成失败: {summary}"
    return True, summary


def stage_enrich(run_dir: Path, yes: bool, force: bool = False) -> tuple[bool, str]:
    """Stage 3: 摘要补全 + 完整性检查。"""
    print(f"\n=== Stage 3 摘要补全 ===")
    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/enrich_abstracts.py"),
         "-i", str(stage_file(run_dir, "merge", "merged_filtered.json")),
         "-o", str(stage_file(run_dir, "enrich", "enriched.json"))],
        run_dir, "enrich",
    )
    if not ok:
        return False, f"摘要补全失败: {summary}"

    # 完整性检查点
    proc = subprocess.run(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/check_abstracts.py"),
         str(stage_file(run_dir, "enrich", "enriched.json"))],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    check_out = (proc.stdout + proc.stderr)
    for line in check_out.splitlines():
        if line.strip():
            print(f"  {line}")
    blocked = proc.returncode != 0

    if blocked:
        choice = ask(
            "摘要缺失率 > 5%。可选：跑 Tier 2（逐篇开详情页补摘要，需要浏览器+会话）或继续",
            [("t", "跑 Tier 2 补摘要（先确认浏览器已认证）"),
             ("c", "继续管道（缺失的摘要不影响评分结构）"),
             ("s", "停止")],
            "t", yes,
        )
        if choice == "t":
            print("  [tier2] 生成任务并提取...")
            # check_abstracts 已生成 task（在 enriched.json 同目录）
            task_path = stage_file(run_dir, "enrich", "tier2_task.json")
            if task_path.exists():
                ok2, s2 = run_cmd(
                    [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/_tier2_extract.py"),
                     "--tasks", str(task_path),
                     "--extractors-dir", str(PROJECT_ROOT / "extractors"),
                     "--output-dir", str(CACHE_DIR)],
                    run_dir, "enrich",
                )
                if ok2:
                    # 合并 tier2 结果回 enriched（tier2_result.json 在 tasks 同目录）
                    ok3, s3 = run_cmd(
                        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/merge_tier2.py"),
                         "-r", str(stage_file(run_dir, "enrich", "tier2_result.json")),
                         "-e", str(stage_file(run_dir, "enrich", "enriched.json")),
                         "-o", str(stage_file(run_dir, "enrich", "enriched.json"))],
                        run_dir, "enrich",
                    )
                    print(f"  [tier2] {s3}")
                else:
                    print(f"  [tier2] 提取失败（可忽略，继续管道）: {s2}")
            else:
                print("  [tier2] 未找到 tier2_task.json，跳过")
        elif choice == "s":
            return False, "用户选择停止（摘要缺失率超阈值）"
    return True, summary


def stage_rank(run_dir: Path, yes: bool, force: bool = False) -> tuple[bool, str]:
    """Stage 4: 确定性评分 → 可选 AI 重排/中文摘要/主题聚类。"""
    print(f"\n=== Stage 4 评分排序 ===")
    concept = load_concept(run_dir)
    topic = concept.get("title") or run_dir.name
    top = int(concept.get("top") or 15)
    print(f"  [top] 输出数量: {top} 篇（可在 webui/init 调整）")

    # 4a. 确定性多维评分（paper_ranker，无 LLM）
    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/paper_ranker.py"),
         "-i", str(stage_file(run_dir, "enrich", "enriched.json")), "-o", str(stage_file(run_dir, "rank", "ranked.json")),
         "-c", str(run_dir / "search_concept.json"), "--mode", "balanced"],
        run_dir, "rank",
    )
    if not ok:
        return False, f"评分失败: {summary}"

    # 4b. 可选 AI 增强
    api_key, base_url, model = get_llm_config()
    ranked_path = str(stage_file(run_dir, "rank", "ranked.json"))
    if not api_key:
        print("  [llm] 未配置 LLM_API_KEY，跳过 AI 重排/中文摘要（评分已完成）")
        ai_input = ranked_path
    else:
        ai_input = ranked_path
        print(f"  [llm] 已配置 ({model or '?'})，执行 AI 重排...")
        ok, summary = run_cmd(
            [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/ai_rerank.py"),
             "-i", ai_input, "-o", str(stage_file(run_dir, "rank", "ai_ranked.json")),
             "-t", topic, "--concurrency", "8"],
            run_dir, "rank",
        )
        if ok:
            ai_input = str(stage_file(run_dir, "rank", "ai_ranked.json"))
        else:
            print(f"  [llm] AI 重排失败（继续用机器评分）: {summary}")

        ok, summary = run_cmd(
            [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/ai_summarize.py"),
             "-i", ai_input, "-o", str(stage_file(run_dir, "rank", "summarized.json")), "--top", str(top)],
            run_dir, "rank",
        )
        if ok:
            ai_input = str(stage_file(run_dir, "rank", "summarized.json"))

    # 4c. 主题聚类（无 LLM 时自动降级为单主题）
    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/theme_cluster.py"),
         "-i", ai_input, "-o", str(stage_file(run_dir, "rank", "themed.json")),
         "-t", topic, "--top", str(top)],
        run_dir, "rank",
    )
    if not ok:
        return False, f"主题聚类失败: {summary}"
    return True, summary


def stage_output(run_dir: Path, yes: bool, force: bool = False) -> tuple[bool, str]:
    """Stage 5: 中文总结摘要 + Markdown + BibTeX。"""
    print(f"\n=== Stage 5 生成输出 ===")
    concept = load_concept(run_dir)
    top = int(concept.get("top") or 15)
    themed = str(stage_file(run_dir, "rank", "themed.json"))
    md_in = themed

    # 先生成总结版中文摘要（缺 _cn_summary 的论文；需 .env 配 LLM_API_KEY）
    # 无 key / 失败时降级：直接用 themed.json 渲染（英文摘要兜底）
    summarized = str(stage_file(run_dir, "output", "summarized.json"))
    ok_sum, summary_sum = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/ai_summarize.py"),
         "-i", themed, "-o", summarized, "--skip-existing"],
        run_dir, "output",
    )
    if ok_sum:
        md_in = summarized
        print(f"    [summary] 中文总结摘要已生成 → {summarized}")
    else:
        print(f"    [summary] 跳过（{summary_sum}）→ 用英文摘要兜底")

    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/make_output.py"),
         "-i", md_in, "-o", str(stage_file(run_dir, "output", "final_results.md")), "--top", str(top)],
        run_dir, "output",
    )
    if not ok:
        return False, f"Markdown 生成失败: {summary}"

    ok, summary = run_cmd(
        [PYTHON, "-X", "utf8", str(PROJECT_ROOT / "scripts/pipeline/export_citations.py"),
         "--bibtex", "--themed", md_in, "-o", str(stage_file(run_dir, "output", "results.bib"))],
        run_dir, "output",
    )
    if not ok:
        return False, f"BibTeX 生成失败: {summary}"
    return True, f"输出完成: final_results.md + results.bib（中文摘要 {('已生成' if ok_sum else '缺失→英文兜底')}）"


STAGE_FUNCS = {
    "health": stage_health,
    "search": stage_search,
    "merge": stage_merge,
    "enrich": stage_enrich,
    "rank": stage_rank,
    "output": stage_output,
}


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def _ask_text(question: str, default: str = "", hint: str = "", allow_empty: bool = False) -> str:
    """交互式问答：显示问题（含默认值/提示），返回用户输入或默认值。

    allow_empty=True 时允许空回车跳过（返回 ""），用于可选字段。
    """
    if default:
        suffix = f" [默认: {default}]"
    elif allow_empty:
        suffix = " [回车跳过]"
    else:
        suffix = ""
    if hint:
        print(f"  · {hint}")
    while True:
        raw = input(f"? {question}{suffix}: ").strip()
        if raw:
            return raw
        if default or allow_empty:
            return default
        print("  ✗ 不能为空，请重新输入")


def _gen_run_id(title: str, concepts: list[str]) -> str:
    """生成 run_id: YYYY-MM-DD_HHMM_<slug>。slug 从标题/关键词提取英文数字。"""
    import re
    src = (title or " ".join(concepts) or "task")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", src).strip("-").lower()
    slug = slug[:40] or "task"
    return f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_{slug}"


def create_task(run_id: str = None, params: dict = None) -> dict:
    """创建任务（纯逻辑，CLI 和 webui 共用）。

    params: {title, core_concepts, synonyms, sub_topics, exclude,
             year_range, databases, target}（缺省用默认值）
    run_id 缺省时自动生成。返回 {run_id, path, created}。
    """
    p = params or {}
    title = str(p.get("title") or "")
    if not run_id:
        run_id = _gen_run_id(title, p.get("core_concepts") or [])
    run_dir = ensure_run_dir(run_id)
    concept_path = run_dir / "search_concept.json"
    if concept_path.exists():
        return {"run_id": run_id, "path": str(concept_path), "created": False}

    core = [str(c).strip() for c in (p.get("core_concepts") or []) if str(c).strip()]
    concept = {
        "title": title,
        "core_concepts": core or ["你的核心关键词"],
        "synonyms": p.get("synonyms") or {},
        "sub_topics": [str(s).strip() for s in (p.get("sub_topics") or []) if str(s).strip()],
        "exclude": [str(s).strip() for s in (p.get("exclude") or []) if str(s).strip()],
        "year_range": str(p.get("year_range") or "2020-2026"),
        "databases": (p.get("databases") or ["openalex"]),
        "target": int(p.get("target") or 40),
        "top": int(p.get("top") or 15),
    }
    concept_path.write_text(json.dumps(concept, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"run_id": run_id, "path": str(concept_path), "created": True}


def cmd_init(run_id: str = None) -> int:
    """交互式创建研究任务：问答生成 search_concept.json（run_id 可选）。"""
    print("\n=== 新建研究任务（问答式，Ctrl+C 可取消）===")
    title = _ask_text("任务标题", hint="用于展示，如: MCU 固件安全综述")

    concepts = _ask_text("核心关键词（逗号分隔）", hint="如: microcontroller, embedded security")
    core = [c.strip() for c in concepts.split(",") if c.strip()]

    synonyms_raw = _ask_text("同义词（可选）", hint="格式: 词=同义1,同义2 | 多组用 | 分隔，直接回车跳过", allow_empty=True)
    synonyms = {}
    for part in synonyms_raw.split("|"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, vs = part.partition("=")
        k = k.strip()
        vals = [v.strip() for v in vs.split(",") if v.strip()]
        if k and vals:
            synonyms[k] = vals

    subs_raw = _ask_text("子方向（可选，逗号分隔）", allow_empty=True)
    subs = [s.strip() for s in subs_raw.split(",") if s.strip()]

    excl_raw = _ask_text("排除词（可选，逗号分隔）", allow_empty=True)
    excl = [s.strip() for s in excl_raw.split(",") if s.strip()]

    year = _ask_text("年份范围", default="2020-2026")

    db_names = ["openalex", "scopus", "ieee", "acm", "engineering_village"]
    print("  数据库（可多选）:")
    for i, n in enumerate(db_names, 1):
        tag = "（免费，无需账号）" if n == "openalex" else "（扩展，需会话）"
        print(f"    [{i}] {n} {tag}")
    dbs_raw = _ask_text("输入编号（逗号分隔）", default="1,2")
    try:
        idxs = [int(x.strip()) for x in dbs_raw.split(",") if x.strip()]
        dbs = [db_names[i - 1] for i in idxs if 1 <= i <= len(db_names)]
    except ValueError:
        dbs = db_names
    if not dbs:
        dbs = db_names

    target_raw = _ask_text("每库目标篇数", default="40")
    try:
        target = int(target_raw)
    except ValueError:
        target = 40

    top_raw = _ask_text("最终输出篇数（评分排序后取前 N）", default="15")
    try:
        top = int(top_raw)
    except ValueError:
        top = 15

    result = create_task(run_id, {
        "title": title,
        "core_concepts": core,
        "synonyms": synonyms,
        "sub_topics": subs,
        "exclude": excl,
        "year_range": year,
        "databases": dbs,
        "target": target,
        "top": top,
    })
    if not result["created"]:
        print(f"[init] 已存在: {result['path']}（保留原内容，不覆盖）")
        return 0
    print(f"\n✓ 已创建任务: {result['run_id']}")
    print(f"  关键词: {', '.join(core)}")
    print(f"  年份: {year} | 数据库: {', '.join(dbs)}")
    print(f"\n  下一步: python main.py run {result['run_id']}")
    return 0


def _task_status(run_dir: Path) -> str:
    """根据产物推断任务状态（去掉 health，它无产物恒完成）。"""
    # 运行中标记优先（main.py 阶段开始时写入）
    sr = run_dir / "stage_report.json"
    if sr.exists():
        try:
            rep = json.loads(sr.read_text(encoding="utf-8"))
            if rep.get("status") == "running":
                return "运行中"
        except Exception:
            pass
    stages = [s for s in STAGES if s["id"] != "health"]
    done = sum(1 for s in stages if stage_done(run_dir, s["id"]))
    if done == len(stages):
        return "全部完成"
    if done == 0:
        if (run_dir / "stage_report.json").exists():
            return "中断/失败"
        return "未运行"
    return f"进行到 {stages[done]['id']}"


def _last_run_time(run_dir: Path) -> str:
    """最后运行时间（优先 stage_report.json 的 ts，其次 run_report.md 修改时间）。"""
    sr = run_dir / "stage_report.json"
    if sr.exists():
        try:
            return json.loads(sr.read_text(encoding="utf-8")).get("ts", "")[:16]
        except Exception:
            pass
    rr = run_dir / "run_report.md"
    if rr.exists():
        try:
            return datetime.fromtimestamp(rr.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return "-"


def list_tasks() -> list[dict]:
    """所有任务数据（webui 和 list 共用）。"""
    if not RUN_DIR_BASE.exists():
        return []
    rows = []
    for d in sorted(RUN_DIR_BASE.iterdir(), reverse=True):
        if not d.is_dir() or not (d / "search_concept.json").exists():
            continue
        try:
            concept = json.loads((d / "search_concept.json").read_text(encoding="utf-8"))
        except Exception:
            concept = {}
        rows.append({
            "run_id": d.name,
            "title": concept.get("title", d.name),
            "status": _task_status(d),
            "last_run": _last_run_time(d),
        })
    return rows


def cmd_list() -> int:
    """列出所有任务 + 状态。"""
    rows = list_tasks()
    if not rows:
        print("[list] 还没有任何任务。创建: python main.py init")
        return 0
    print(f"\n共 {len(rows)} 个任务\n")
    print(f"{'run_id':<30} {'状态':<10} {'最后运行':<17} 标题")
    print("-" * 88)
    for r in rows:
        print(f"{r['run_id']:<30} {r['status']:<10} {r['last_run']:<17} {r['title']}")
    print()
    return 0


def task_detail(run_id: str) -> dict | None:
    """任务详情数据（webui 和 show 共用）。找不到返回 None。"""
    run_dir = RUN_DIR_BASE / run_id
    concept_path = run_dir / "search_concept.json"
    if not concept_path.exists():
        return None
    concept = json.loads(concept_path.read_text(encoding="utf-8"))
    steps = []
    for stage in STAGES:
        sid = stage["id"]
        if sid == "health":
            # 健康检查无产物：显示最近一次 health 运行是否成功
            sr = run_dir / "stage_report.json"
            done = False
            if sr.exists():
                try:
                    rep = json.loads(sr.read_text(encoding="utf-8"))
                    done = rep.get("stage") == "health" and rep.get("ok")
                except Exception:
                    done = False
        else:
            done = stage_done(run_dir, sid)
        steps.append({
            "id": sid,
            "name": stage["name"],
            "dir": STAGE_DIRS[sid],
            "done": done,
        })
    detail = {
        "run_id": run_id,
        "concept": concept,
        "steps": steps,
        "stage_report": None,
        "outputs": [],
    }
    sr = run_dir / "stage_report.json"
    if sr.exists():
        try:
            detail["stage_report"] = json.loads(sr.read_text(encoding="utf-8"))
        except Exception:
            pass
    outs = []
    for f in ["final_results.md", "results.bib", "themed.json"]:
        p = run_dir / f
        if not p.exists():
            p = run_dir / STAGE_DIRS["output" if f != "themed.json" else "rank"] / f
        if p.exists():
            outs.append({"name": f, "size": p.stat().st_size})
    detail["outputs"] = outs

    # 论文总数：评分排序后的全量（ranked → enriched → merged_filtered 依次回退）
    total_papers = None
    for cand_name in ["ranked.json", "enriched.json", "merged_filtered.json"]:
        cand = run_dir / STAGE_DIRS["rank" if cand_name == "ranked.json"
                                    else ("enrich" if cand_name == "enriched.json" else "merge")] / cand_name
        if cand.exists():
            try:
                d = json.loads(cand.read_text(encoding="utf-8"))
                ps = d.get("papers", d) if isinstance(d, dict) else d
                if isinstance(ps, list):
                    total_papers = len(ps)
                    break
            except Exception:
                pass
    detail["total_papers"] = total_papers
    return detail


def cmd_show(run_id: str) -> int:
    """查看任务详情。"""
    detail = task_detail(run_id)
    if detail is None:
        print(f"[show] 找不到任务: {run_id}（可用 python main.py list 查看）", file=sys.stderr)
        return 1
    concept = detail["concept"]
    print(f"\n=== 任务: {run_id} ===")
    print(f"标题:    {concept.get('title', '-')}")
    print(f"关键词:  {', '.join(concept.get('core_concepts', []))}")
    if concept.get("synonyms"):
        print(f"同义词:  {json.dumps(concept['synonyms'], ensure_ascii=False)}")
    if concept.get("sub_topics"):
        print(f"子方向:  {', '.join(concept['sub_topics'])}")
    if concept.get("exclude"):
        print(f"排除:    {', '.join(concept['exclude'])}")
    print(f"年份:    {concept.get('year_range', '-')}")
    print(f"数据库:  {', '.join(concept.get('databases', []))}")
    print(f"目标:    {concept.get('target', '-')} 篇/库")
    print("\n阶段进度:")
    for s in detail["steps"]:
        print(f"  [{'✓' if s['done'] else '·'}] {s['name']}")
    rep = detail["stage_report"]
    if rep:
        ok_str = "成功" if rep.get("ok") else "失败"
        print(f"\n最后运行: {rep.get('ts', '?')} — {rep.get('stage', '?')} {ok_str}: {rep.get('message', '')}")
    if detail["outputs"]:
        outs = [f"{o['name']} ({max(1, o['size'] // 1024)}KB)" for o in detail["outputs"]]
        print(f"产物:    {', '.join(outs)}")
    print()
    return 0


def cmd_run(run_id: str, from_stage: str, to_stage: str, yes: bool, force: bool):
    """状态机主流程。"""
    run_dir = ensure_run_dir(run_id)
    if not (run_dir / "search_concept.json").exists():
        print(f"[ERROR] 缺少 search_concept.json。先运行: python main.py init {run_id}", file=sys.stderr)
        return 2

    ids = [s["id"] for s in STAGES]
    if from_stage not in ids or to_stage not in ids:
        print(f"[ERROR] 未知阶段。可用: {', '.join(ids)}", file=sys.stderr)
        return 2
    if ids.index(from_stage) > ids.index(to_stage):
        print(f"[ERROR] --from 必须在 --to 之前", file=sys.stderr)
        return 2

    print(f"══════════════════════════════════════════")
    print(f"  paperflow 管道  run_id={run_id}")
    print(f"  范围: {from_stage} → {to_stage}" + ("  (非交互)" if yes else ""))
    print(f"══════════════════════════════════════════")

    report_lines = []
    start_all = time.time()

    for stage in STAGES:
        if ids.index(stage["id"]) < ids.index(from_stage) or ids.index(stage["id"]) > ids.index(to_stage):
            continue
        sid, sname = stage["id"], stage["name"]

        if stage_done(run_dir, sid) and not force:
            print(f"\n[skip] Stage {ids.index(sid)} {sname}: 产物已存在（--force 可重跑）")
            report_lines.append(f"- [✓] Stage {ids.index(sid)} **{sname}** — 已存在，跳过")
            continue

        print(f"\n── Stage {ids.index(sid)}: {sname} ──")
        # 阶段开始先写"运行中"标记（webui 轮询据此显示当前阶段，避免滞后一个阶段）
        write_stage_report(run_dir, sid, False, "运行中", status="running")
        try:
            ok, msg = STAGE_FUNCS[sid](run_dir, yes, force)
        except KeyboardInterrupt:
            print(f"\n[中断] 用户 Ctrl+C")
            write_stage_report(run_dir, sid, False, "用户中断")
            update_run_report(run_dir, report_lines + [f"- [✗] Stage {ids.index(sid)} **{sname}** — 用户中断"])
            return 1
        except Exception as e:
            ok, msg = False, f"异常: {type(e).__name__}: {e}"
            print(f"  [ERROR] {msg}")

        write_stage_report(run_dir, sid, ok, msg)

        if ok:
            report_lines.append(f"- [✓] Stage {ids.index(sid)} **{sname}** — {msg}")
            print(f"  ✓ {msg}")
        else:
            report_lines.append(f"- [✗] Stage {ids.index(sid)} **{sname}** — {msg}")
            update_run_report(run_dir, report_lines)
            print(f"\n  ✗ Stage {sid} 失败: {msg}")
            print(f"\n  ── 下一步建议 ──")
            print(f"    • 查看详细日志: {stage_file(run_dir, sid, 'log.txt')}")
            print(f"    • 修复后重跑:   python main.py run {run_id} --from {sid}")
            print(f"    • 回退到某步:   python main.py rollback {run_id} --to <step>")
            print(f"    • 运行报告:     {run_dir / 'run_report.md'}")
            return 1

    elapsed = time.time() - start_all
    report_lines.append(f"\n**总耗时: {elapsed:.0f}s**")
    update_run_report(run_dir, report_lines)
    print(f"\n══════════════════════════════════════════")
    print(f"  全部完成 ({elapsed:.0f}s)")
    print(f"  输出: {stage_file(run_dir, 'output', 'final_results.md')}")
    print(f"  报告: {run_dir / 'run_report.md'}")
    print(f"══════════════════════════════════════════")
    return 0


def cmd_report(run_id: str):
    """查看运行报告。"""
    run_dir = RUN_DIR_BASE / run_id
    path = run_dir / "run_report.md"
    if path.exists():
        print(path.read_text(encoding="utf-8"))
        return 0
    print(f"[report] 还没有运行报告: {path}", file=sys.stderr)
    return 1


def cmd_rollback(run_id: str, to_stage: str) -> int:
    """回退到某一步：删除该步及其之后的所有 step 文件夹 + 状态文件。"""
    ids = [s["id"] for s in STAGES]
    if to_stage not in ids or to_stage == "health":
        print(f"[rollback] --to 必须是 {' / '.join(ids[1:])}", file=sys.stderr)
        return 2
    run_dir = RUN_DIR_BASE / run_id
    if not run_dir.exists():
        print(f"[rollback] 找不到任务: {run_id}", file=sys.stderr)
        return 1
    idx = ids.index(to_stage)
    removed = []
    for s in STAGES[idx:]:
        d = run_dir / STAGE_DIRS[s["id"]]
        if d.exists():
            shutil.rmtree(d)
            removed.append(d.name)
    for f in ["stage_report.json", "run_report.md"]:
        p = run_dir / f
        if p.exists():
            p.unlink()
            removed.append(f)
    if not removed:
        print(f"[rollback] {run_id} 在 {to_stage} 及之后没有产物")
        print(f"           旧格式平铺任务请用: python main.py run {run_id} --from {to_stage} --force")
        return 0
    print(f"[rollback] 已回退到 {to_stage}，删除: {', '.join(removed)}")
    print(f"  重跑: python main.py run {run_id} --from {to_stage}")
    return 0


def cmd_delete(run_id: str, yes: bool = False) -> int:
    """删除任务：移除整个 run 目录（search_concept.json + 全部产物）。

    只删该任务自己的目录；共享摘要缓存（paper-abstracts/）跨任务复用，不删。
    路径安全：run_id 必须解析到 RUN_DIR_BASE 内部。
    """
    run_dir = RUN_DIR_BASE / run_id
    try:
        base = RUN_DIR_BASE.resolve()
        target = run_dir.resolve()
        if not target.is_relative_to(base):
            print(f"[delete] 非法任务 ID: {run_id}", file=sys.stderr)
            return 2
    except (OSError, ValueError):
        print(f"[delete] 非法任务 ID: {run_id}", file=sys.stderr)
        return 2
    if not (run_dir / "search_concept.json").exists():
        print(f"[delete] 找不到任务: {run_id}（可用 python main.py list 查看）", file=sys.stderr)
        return 1
    try:
        concept = json.loads((run_dir / "search_concept.json").read_text(encoding="utf-8"))
    except Exception:
        concept = {}
    title = concept.get("title", run_id)
    if not yes:
        choice = ask(
            f"确认删除任务「{title}」（{run_id}）？\n将删除整个 run 目录（含全部产物），不可恢复",
            [("y", "删除"), ("n", "取消")], "n", False,
        )
        if choice != "y":
            print("[delete] 已取消")
            return 0
    shutil.rmtree(run_dir)
    print(f"[delete] 已删除任务: {run_id}（{title}）")
    return 0


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="paperflow standalone 管道")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="交互式创建研究任务")
    p_init.add_argument("run_id", nargs="?", help="任务 ID（可选，不填自动生成）")

    p_list = sub.add_parser("list", help="列出所有任务和状态")

    p_show = sub.add_parser("show", help="查看任务详情")
    p_show.add_argument("run_id")

    p_launch = sub.add_parser("launch", help="启动浏览器（访问引导）")
    p_launch.add_argument("--open", action="append", help="启动后打开的 URL（可重复传）")

    p_health = sub.add_parser("health", help="只跑健康检查")
    p_health.add_argument("run_id")

    p_run = sub.add_parser("run", help="跑管道")
    p_run.add_argument("run_id")
    p_run.add_argument("--from", dest="from_stage", default="health", help="起始阶段")
    p_run.add_argument("--to", dest="to_stage", default="output", help="结束阶段")
    p_run.add_argument("--yes", action="store_true", help="非交互模式（决策点用默认策略）")
    p_run.add_argument("--force", action="store_true", help="强制重跑已完成的阶段")

    p_rep = sub.add_parser("report", help="查看运行报告")
    p_rep.add_argument("run_id")

    p_rollback = sub.add_parser("rollback", help="回退到某一步（删除该步及之后的产物）")
    p_rollback.add_argument("run_id")
    p_rollback.add_argument("--to", required=True, help="回退到哪个阶段（如 merge）")

    p_delete = sub.add_parser("delete", help="删除任务（整个 run 目录 + 全部产物，不可恢复）")
    p_delete.add_argument("run_id")
    p_delete.add_argument("--yes", action="store_true", help="跳过确认直接删除")

    p_migrate = sub.add_parser("migrate", help="迁移旧缓存格式到新格式（paper-abstracts/）")
    p_migrate.add_argument("--dry-run", action="store_true", help="只预览不实际改动")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    if args.command == "init":
        return cmd_init(args.run_id)
    if args.command == "list":
        return cmd_list()
    if args.command == "show":
        return cmd_show(args.run_id)
    if args.command == "launch":
        import launch_browser
        return launch_browser.launch(open_urls=args.open or [])
    if args.command == "health":
        run_dir = ensure_run_dir(args.run_id)
        ok, msg = stage_health(run_dir, yes=True)
        write_stage_report(run_dir, "health", ok, msg)
        print(f"\n{'✓' if ok else '✗'} {msg}")
        return 0 if ok else 1
    if args.command == "run":
        return cmd_run(args.run_id, args.from_stage, args.to_stage, args.yes, args.force)
    if args.command == "report":
        return cmd_report(args.run_id)
    if args.command == "rollback":
        return cmd_rollback(args.run_id, args.to)
    if args.command == "delete":
        return cmd_delete(args.run_id, args.yes)
    if args.command == "migrate":
        return cmd_migrate(args.dry_run)
    return 0


def cmd_migrate(dry_run: bool = False):
    """旧缓存格式 → 新格式迁移（cache_utils.migrate_legacy 唯一入口）。"""
    from utils.cache_utils import migrate_legacy
    stats = migrate_legacy(dry_run=dry_run)
    print(f"[migrate] converted={len(stats['converted'])} "
          f"deleted={len(stats['deleted'])} kept={stats['kept']} "
          f"failed={len(stats['failed'])}")
    for c in stats["converted"][:20]:
        print(f"  ✓ {c}")
    for d in stats["deleted"][:20]:
        print(f"  ✗ 删除 {d}")
    print(f"[migrate] {'（dry-run，未实际改动）' if dry_run else '完成'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
