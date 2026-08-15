"""Scopus search — API-based, no browser needed.
用法:
  python _search_scopus.py --query-file memory/scopus_query.txt --target 40 --output memory/scopus_results.json
"""
import sys, os, json, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scopus_search import search_all

# standalone 版：key 从 .env 读取（SCOPUS_API_KEY），config.yaml 兜底
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.env_config import get_scopus_api_key, load_dotenv
load_dotenv()

API_KEY = get_scopus_api_key()
if not API_KEY:
    sys.stderr.write("[scopus] 未配置 Scopus API key（.env 的 SCOPUS_API_KEY）\n")
    sys.exit(2)

p = argparse.ArgumentParser()
p.add_argument("--query-file", required=True, help="path to scopus_query.txt")
p.add_argument("--target", type=int, default=40)
p.add_argument("--output", required=True, help="output JSON path")
p.add_argument("--timeout", type=int, default=20)
p.add_argument("--delay", type=float, default=0.3)
args = p.parse_args()

with open(args.query_file, "r", encoding="utf-8") as f:
    query = f.read().strip()

result = search_all(
    query=query,
    api_key=API_KEY,
    target_count=args.target,
    timeout=args.timeout,
    delay=args.delay,
)

with open(args.output, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

sys.stderr.write(f"[scopus] {result['count']} papers (total: {result['total_results']}) [{result.get('pages_fetched',1)} pages] -> {args.output}\n")

if result["count"] == 0:
    print(f"DONE|scopus|0|EMPTY")
else:
    print(f"DONE|scopus|{result['count']}|ok")
