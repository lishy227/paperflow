#!/usr/bin/env python3
"""
webui.py — paid-db-access 本地 Web 控制台（零依赖）
=================================================

用 Python 标准库 http.server 提供 Web 界面，分步执行/回退管道。

用法:
    python webui.py [--port 8080] [--no-browser]

功能:
    - 任务列表 / 新建（表单） / 详情
    - 6 步步骤条：单步执行、跑全部、回退到此步
    - 运行中自动刷新进度（读 stage_report.json + 每步 log.txt）
    - 停止任务（taskkill 进程树）
    - 健康检查（复用 scripts/health_check.py）
    - 结果预览 + BibTeX 下载

架构:
    - 列表/详情/创建/回退: import main.py 的函数直接调（快）
    - 运行: 独立子进程跑 main.py（可停止、不阻塞 web 服务）
    - 状态: 读文件（stage_report.json / step 文件夹 / log.txt）
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
for _p in (str(ROOT), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils.encoding import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

import main as m  # noqa: E402  (main.py 的 list_tasks/task_detail/create_task/STAGE_DIRS 等)

print(f"[webui] loaded main from: {m.__file__}", file=sys.stderr)

WEBUI_DIR = ROOT / "webui"
RUN_LOG = ROOT / "memory" / "webui_run.log"

DB_NAMES = ["openalex", "scopus", "ieee", "acm", "engineering_village"]
FREE_DBS = {"openalex"}
STAGE_IDS = [s["id"] for s in m.STAGES]


# ─────────────────────────────────────────────────────────────
# 进程管理（单任务锁：同一时间只跑一个管道）
# ─────────────────────────────────────────────────────────────

class TaskRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.run_id: str | None = None
        self.started_at: str | None = None

    def start(self, run_id: str, from_stage: str, to_stage: str, force: bool = False) -> tuple[bool, str]:
        with self._lock:
            if self.is_running():
                return False, f"已有任务在运行: {self.run_id}（先停止或等待完成）"
            RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
            cmd = [sys.executable, "-X", "utf8", str(ROOT / "main.py"), "run", run_id,
                   "--from", from_stage, "--to", to_stage, "--yes"]
            if force:
                cmd.append("--force")
            with open(RUN_LOG, "a", encoding="utf-8") as logf:
                logf.write(f"\n===== {m.now_str()} $ {' '.join(cmd)} =====\n")
                self.proc = subprocess.Popen(
                    cmd, cwd=str(ROOT),
                    stdout=logf, stderr=subprocess.STDOUT,
                )
            self.run_id = run_id
            self.started_at = m.now_str()
            return True, f"已启动 {run_id}（{from_stage} → {to_stage}）"

    def stop(self) -> bool:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True)
                self.proc = None
                return True
            return False

    def is_running(self) -> bool:
        if self.proc is None:
            return False
        if self.proc.poll() is not None:
            self.proc = None  # 已结束，清理引用
            return False
        return True

    def info(self) -> dict:
        return {"running": self.is_running(), "run_id": self.run_id, "started_at": self.started_at}


runner = TaskRunner()


# ─────────────────────────────────────────────────────────────
# 数据辅助
# ─────────────────────────────────────────────────────────────

def _run_dir(run_id: str) -> Path:
    return m.RUN_DIR_BASE / run_id


def _log_tail(run_id: str, stage: str | None, max_lines: int = 40) -> str:
    """某阶段 log.txt 尾部（含旧平铺 log_<stage>.txt 兼容）。"""
    if not stage:
        return ""
    candidates = [
        _run_dir(run_id) / m.STAGE_DIRS.get(stage, stage) / "log.txt",
        _run_dir(run_id) / f"log_{stage}.txt",
    ]
    for p in candidates:
        if p.exists():
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(lines[-max_lines:])
            except Exception:
                return ""
    return ""


def task_status(run_id: str) -> dict | None:
    """任务状态（webui 轮询用）。"""
    detail = m.task_detail(run_id)
    if detail is None:
        return None
    info = runner.info()
    running_here = info["running"] and info["run_id"] == run_id
    cur_stage = None
    if detail["stage_report"]:
        # 阶段开始时已写"运行中"标记，report.stage 即当前阶段（不再滞后）
        cur_stage = detail["stage_report"].get("stage")
    elif running_here:
        # report 还没写（进程刚启动）→ 从下一个未完成步骤推断
        for s in detail["steps"]:
            if not s["done"]:
                cur_stage = s["id"]
                break
    return {
        "running": running_here,
        "current_stage": cur_stage,
        "log_tail": _log_tail(run_id, cur_stage),
        "detail": detail,
        "runner": info,
    }


def read_result(run_id: str, name: str) -> str | None:
    """读结果文件内容（新 step 目录 + 旧平铺兼容）。"""
    stage = "output" if name != "themed.json" else "rank"
    candidates = [
        _run_dir(run_id) / m.STAGE_DIRS[stage] / name,
        _run_dir(run_id) / name,
    ]
    for p in candidates:
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return None
    return None


# ─────────────────────────────────────────────────────────────
# HTTP 处理
# ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "paid-db-access-webui/1.0"

    # ---- 基础 ----
    def _send_download(self, content: str, filename: str, ctype: str = "text/plain; charset=utf-8"):
        """带 Content-Disposition: attachment 的文件下载响应（浏览器直接保存，不显示）。"""
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._serve_index()
        elif path == "/api/tasks":
            self._json(200, {"tasks": m.list_tasks()})
        elif path.startswith("/api/tasks/"):
            self._handle_task_get(path)
        elif path == "/api/health":
            self._json(200, {"hint": "健康检查请用 POST /api/health"})
        elif path == "/api/debug":
            import sys as _sys
            self._json(200, {
                "main_file": m.__file__,
                "task_detail_line": m.task_detail.__code__.co_firstlineno,
                "stage_done_line": m.stage_done.__code__.co_firstlineno,
                "sys_path": _sys.path[:6],
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/tasks":
            self._handle_create()
        elif path == "/api/health":
            self._handle_health()
        elif path.startswith("/api/tasks/"):
            self._handle_task_post(path)
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/tasks/"):
            self._handle_task_delete(path)
        else:
            self._json(404, {"error": "not found"})

    # ---- 实现 ----
    def _serve_index(self):
        idx = WEBUI_DIR / "index.html"
        if not idx.exists():
            self._send(500, b"webui/index.html not found")
            return
        body = idx.read_bytes()
        self._send(200, body, "text/html; charset=utf-8")

    def _split_task_path(self, path: str):
        """/api/tasks/<run_id>/<action> → (run_id, action)。run_id 含特殊字符需 URL 解码。"""
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            return None, None
        run_id = urllib.parse.unquote(parts[2])
        action = parts[3] if len(parts) > 3 else None
        return run_id, action

    def _handle_task_get(self, path: str):
        run_id, action = self._split_task_path(path)
        if not run_id:
            self._json(400, {"error": "bad path"})
            return
        if action is None:
            detail = m.task_detail(run_id)
            if detail is None:
                self._json(404, {"error": f"task not found: {run_id}"})
            else:
                self._json(200, detail)
        elif action == "status":
            st = task_status(run_id)
            if st is None:
                self._json(404, {"error": f"task not found: {run_id}"})
            else:
                self._json(200, st)
        elif action == "result":
            content = read_result(run_id, "final_results.md")
            if content is None:
                self._json(404, {"error": "final_results.md not found"})
            else:
                self._send(200, content.encode("utf-8"), "text/markdown; charset=utf-8")
        elif action == "result.bib":
            content = read_result(run_id, "results.bib")
            if content is None:
                self._json(404, {"error": "results.bib not found"})
            else:
                import re as _re
                safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", run_id) or "results"
                self._send_download(content, f"results_{safe_id}.bib")
        elif action == "result.md":
            content = read_result(run_id, "final_results.md")
            if content is None:
                self._json(404, {"error": "final_results.md not found"})
            else:
                import re as _re
                safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", run_id) or "final_results"
                self._send_download(content, f"final_results_{safe_id}.md", "text/markdown; charset=utf-8")
        else:
            self._json(404, {"error": f"unknown action: {action}"})

    def _handle_create(self):
        body = self._read_body()
        title = body.get("title", "")
        if not title:
            self._json(400, {"error": "title 必填"})
            return
        params = {
            "title": title,
            "core_concepts": body.get("core_concepts", []),
            "synonyms": body.get("synonyms", {}),
            "sub_topics": body.get("sub_topics", []),
            "exclude": body.get("exclude", []),
            "year_range": body.get("year_range", "2020-2026"),
            "databases": body.get("databases", ["openalex"]),
            "target": body.get("target", 40),
        }
        result = m.create_task(None, params)
        if result["created"]:
            self._json(200, {"ok": True, "run_id": result["run_id"]})
        else:
            self._json(200, {"ok": False, "run_id": result["run_id"], "error": "已存在"})

    def _handle_health(self):
        import health_check
        report = health_check.run_all()
        self._json(200, report)

    def _handle_task_post(self, path: str):
        run_id, action = self._split_task_path(path)
        if not run_id or not action:
            self._json(400, {"error": "bad path"})
            return
        body = self._read_body()

        if action == "run":
            from_stage = body.get("from_stage") or "health"
            to_stage = body.get("to_stage") or "output"
            force = bool(body.get("force", False))
            if from_stage not in STAGE_IDS or to_stage not in STAGE_IDS:
                self._json(400, {"error": f"无效阶段: {from_stage}/{to_stage}"})
                return
            ok, msg = runner.start(run_id, from_stage, to_stage, force)
            self._json(200 if ok else 409, {"ok": ok, "message": msg})
        elif action == "stop":
            ok = runner.stop()
            self._json(200, {"ok": ok, "message": "已停止" if ok else "没有运行中的任务"})
        elif action == "rollback":
            to_stage = body.get("to")
            if to_stage not in STAGE_IDS or to_stage == "health":
                self._json(400, {"error": f"无效回退目标: {to_stage}"})
                return
            code = m.cmd_rollback(run_id, to_stage)
            self._json(200 if code == 0 else 400, {"ok": code == 0})
        elif action == "open-browser":
            self._handle_open_browser(run_id)
        elif action == "health":
            self._handle_task_health(run_id)
        elif action == "config":
            self._handle_task_config(run_id, body)
        else:
            self._json(404, {"error": f"unknown action: {action}"})

    def _handle_open_browser(self, run_id: str):
        """一键打开浏览器登录：检测 Chrome/Edge → 启动/复用 CDP → 打开任务选中的付费库主页。"""
        detail = m.task_detail(run_id)
        if detail is None:
            self._json(404, {"error": f"task not found: {run_id}"})
            return
        dbs = detail["concept"].get("databases", []) or []
        paid = [d for d in dbs if d != "openalex"]
        if not paid:
            self._json(200, {
                "ok": True, "needed": False,
                "message": "该任务仅使用免费源（OpenAlex），纯 API 无需浏览器登录",
                "urls": [],
            })
            return

        import launch_browser
        from utils.env_config import get_config

        # 1. Chrome/Edge 检测
        browser = launch_browser.find_browser()
        if not browser:
            self._json(200, {
                "ok": False, "error": "未检测到 Chrome/Edge 浏览器",
                "hint": "请安装 Chrome 后重试（下载: https://www.google.com/chrome/）",
            })
            return

        # 2. 取付费库主页 URL（优先 login_url，其次 base_url）
        cfg = (get_config() or {}).get("databases", {})
        urls, opened = [], []
        for db in paid:
            info = cfg.get(db, {}) or {}
            u = info.get("login_url") or info.get("base_url")
            if u:
                urls.append(u)
                opened.append(db)

        # 3. 启动/复用浏览器 + 打开页面（launch 内部处理 CDP 连接）
        code = launch_browser.launch(open_urls=urls)
        if code != 0:
            self._json(200, {"ok": False, "error": "浏览器启动失败（详见 webui 终端日志）"})
            return

        names = [f"{d}({u})" for d, u in zip(opened, urls)]
        self._json(200, {
            "ok": True, "needed": True,
            "message": f"已在浏览器打开 {len(opened)} 个数据库主页，请在浏览器中完成登录：\n" + "\n".join(names),
            "urls": urls,
            "browser": browser,
        })

    def _handle_task_health(self, run_id: str):
        """按任务选择的数据库跑健康检查（免费源任务自动跳过 key/浏览器/登录态）。"""
        detail = m.task_detail(run_id)
        if detail is None:
            self._json(404, {"error": f"task not found: {run_id}"})
            return
        dbs = detail["concept"].get("databases") or None
        import health_check
        report = health_check.run_all(databases=dbs)
        self._json(200, report)

    def _handle_task_config(self, run_id: str, body: dict):
        """更新任务配置（目前支持 top=输出篇数）。写入 search_concept.json，跑管道时生效。"""
        top = body.get("top")
        if top is None:
            self._json(400, {"error": "缺少 top 字段"})
            return
        try:
            top = int(top)
        except (TypeError, ValueError):
            self._json(400, {"error": "top 必须是整数"})
            return
        if not (1 <= top <= 500):
            self._json(400, {"error": "top 范围 1-500"})
            return
        concept_path = m.RUN_DIR_BASE / run_id / "search_concept.json"
        if not concept_path.exists():
            self._json(404, {"error": f"task not found: {run_id}"})
            return
        try:
            concept = json.loads(concept_path.read_text(encoding="utf-8"))
        except Exception:
            self._json(500, {"error": "search_concept.json 读取失败"})
            return
        concept["top"] = top
        concept_path.write_text(json.dumps(concept, ensure_ascii=False, indent=2), encoding="utf-8")
        self._json(200, {"ok": True, "top": top})

    def _handle_task_delete(self, path: str):
        """DELETE /api/tasks/<run_id> — 删除任务（整个 run 目录）。"""
        run_id, action = self._split_task_path(path)
        if not run_id or action is not None:
            self._json(400, {"ok": False, "error": "bad path（DELETE 只支持 /api/tasks/<run_id>）"})
            return
        info = runner.info()
        if info["running"] and info["run_id"] == run_id:
            self._json(409, {"ok": False, "error": "任务正在运行，请先停止再删除"})
            return
        code = m.cmd_delete(run_id, yes=True)
        if code == 0:
            self._json(200, {"ok": True})
        elif code == 2:
            self._json(400, {"ok": False, "error": "非法任务 ID"})
        else:
            self._json(404, {"ok": False, "error": f"任务不存在: {run_id}"})


# ─────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="paid-db-access Web 控制台")
    parser.add_argument("--port", type=int, default=8080, help="端口（默认 8080）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    if not (WEBUI_DIR / "index.html").exists():
        print(f"[webui] 缺少 {WEBUI_DIR / 'index.html'}", file=sys.stderr)
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"[webui] 已启动: {url}")
    print(f"[webui] 停止: Ctrl+C（运行中的任务会继续在后台跑，可重开 webui 查看）")
    if not args.no_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[webui] 退出")


if __name__ == "__main__":
    main()
