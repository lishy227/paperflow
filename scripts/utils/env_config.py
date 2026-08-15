#!/usr/bin/env python3
"""
env_config.py — 统一配置加载（standalone 版，零 openclaw 依赖）
=============================================================

职责：
  1. 定位项目根目录（任何脚本 import 它都能拿到正确路径）
  2. 极简 .env 解析（不依赖 python-dotenv）
  3. 提供 CDP 端口 / API key / LLM 配置的统一读取入口
  4. config.yaml 兜底加载（数据库 URL 等静态配置）

配置优先级：环境变量 > .env 文件 > config.yaml > 默认值

.env 文件位于项目根目录，格式：
    SCOPUS_API_KEY=xxx
    LLM_API_KEY=xxx
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=deepseek-chat
    CDP_PORT=18800
"""

import os
from pathlib import Path

# 项目根目录 = scripts/utils/ 的上三级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

ENV_PATH = PROJECT_ROOT / ".env"


def load_dotenv(path=None) -> bool:
    """极简 .env 解析。已存在的环境变量不覆盖（环境变量优先）。

    Args:
        path: .env 文件路径，默认项目根目录下的 .env

    Returns:
        True 表示 .env 存在并已加载
    """
    p = Path(path) if path else ENV_PATH
    if not p.exists():
        return False
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def get_env(key: str, default: str = "") -> str:
    """读取环境变量（自动先加载 .env）。"""
    load_dotenv()
    return os.environ.get(key, default)


def get_cdp_port() -> int:
    """CDP 调试端口。"""
    try:
        return int(get_env("CDP_PORT", "18800"))
    except ValueError:
        return 18800


def get_cdp_base() -> str:
    """CDP HTTP 入口。"""
    return f"http://127.0.0.1:{get_cdp_port()}"


def get_scopus_api_key() -> str:
    """Scopus API key：只从环境变量/.env 读取（key 统一放 .env）。"""
    return get_env("SCOPUS_API_KEY", "").strip()


def get_llm_config():
    """LLM 配置：返回 (api_key, base_url, model) 或 (None, None, None)。"""
    api_key = get_env("LLM_API_KEY", "").strip()
    base_url = get_env("LLM_BASE_URL", "").strip()
    model = get_env("LLM_MODEL", "").strip()
    if api_key and base_url:
        return api_key, base_url, model or None
    return None, None, None


def get_config() -> dict:
    """加载 config.yaml（数据库 URL、提取器路径等静态配置）。"""
    try:
        import yaml
    except ImportError:
        return {}
    if not (PROJECT_ROOT / "config.yaml").exists():
        return {}
    try:
        with open(PROJECT_ROOT / "config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def project_path(*parts: str) -> Path:
    """项目根下的路径。"""
    return PROJECT_ROOT.joinpath(*parts)


if __name__ == "__main__":
    load_dotenv()
    print(f"PROJECT_ROOT = {PROJECT_ROOT}")
    print(f"CDP_BASE     = {get_cdp_base()}")
    print(f"SCOPUS_KEY   = {'已配置' if get_scopus_api_key() else '未配置'}")
    key, url, model = get_llm_config()
    print(f"LLM          = {'已配置 (' + (model or '?') + ' @ ' + (url or '?') + ')' if key else '未配置'}")
