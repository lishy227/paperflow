# paid-db-access — standalone 版

通过浏览器借用机构登录态，自动搜索四个付费学术数据库
（IEEE / Scopus / ACM / Engineering Village），去重、补摘要、评分、聚类，
输出结构化文献报告 + BibTeX。**纯 Python 项目，零 OpenClaw/agent 依赖，
自带 Web 控制台和 CLI 两套操作方式，报错管理内置。**

> 🆓 **没账号也能体验**：内置 OpenAlex 免费源（纯 API，无需 key、无需浏览器、无需登录态）。
> 新建任务默认就是免费源，5 分钟跑通全管道；有机构账号的人勾选付费库即可升级。

## 快速开始（新机器 / 首次使用）

### 方式一：无账号体验（免费源，推荐先跑这个）

```bash
# 1. 安装依赖（只有 requests/yaml 等基础库）
pip install -r requirements.txt

# 2. 打开 Web 控制台（会自动打开浏览器）
python webui.py

# 3. 新建任务 → 数据库默认勾选免费源（OpenAlex）→ 创建 → 跑全部管道
#    无需 .env、无需浏览器登录、无需任何 key

# 或 CLI：
python main.py init      # 问答式创建（数据库默认 1 = openalex）
python main.py run <run_id> --yes
```

### 方式二：完整能力（付费库，需机构登录态）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 key（所有 key 只放 .env）
copy .env.example .env
#    编辑 .env：SCOPUS_API_KEY 必填；LLM_API_KEY 可选（AI 重排/中文摘要用）

# 3. 启动浏览器 + 各库登录（首次需要，登录态持久化在 .browser-profile/）
#    最简单：双击 open-databases.bat（自动检测 Python/依赖，打开各库主页 + 健康检查）
#    或手动：python launch_browser.py --open https://ieeexplore.ieee.org --open https://dl.acm.org
#    在弹出的浏览器里登录 IEEE / Scopus / ACM / EV（机构账号，SSO 过期需重登）

# 4. 打开 Web 控制台（推荐）或直接用 CLI
python webui.py            # 自动打开 http://127.0.0.1:8080
#   CLI 替代：
#   python main.py init    # 问答式创建任务（选付费库编号）
#   python main.py run <run_id>
```

## 使用方式一：Web 控制台（推荐）

`python webui.py` 启动本地服务（零依赖 http.server），浏览器访问：

- **任务列表**：卡片 + 状态徽章（全部完成 / 进行到 X / 未运行 / 失败）+ **🗑 删除**（确认后移除整个 run 目录）
- **新建任务**：表单（标题 / 关键词 / 同义词 / 子方向 / 排除 / 年份 / **数据库分组选择** / 目标篇数）
  - 数据库分两组：**免费源（OpenAlex，无需账号，默认勾选）** + **付费库（需登录态）**
- **详情页 6 步步骤条**：每步状态圆点 + **▶ 执行**（单步跑）+ **↩ 回退到此**（删该步及之后产物）+ **▶ 跑全部管道**
- **实时进度**：2 秒轮询，当前步骤 + 该步日志尾部
- **停止任务**（taskkill 进程树）、**删除任务**（运行中会拒绝）、**结果预览**（final_results.md）、**BibTeX 下载**、**健康检查**

Web 前端只做两件事：读文件展示状态 + 以子进程方式调用 `main.py`。管道本身全部由 CLI 驱动，Web 不参与计算。

## 使用方式二：CLI

| 命令 | 用途 |
|------|------|
| `python main.py init [run_id]` | 交互式创建任务（run_id 可选，自动生成 `日期_时间_主题`；数据库默认免费源） |
| `python main.py list` | 列出所有任务 + 状态 |
| `python main.py show <run_id>` | 查看任务详情（关键词/步骤进度/最后运行/产物） |
| `python main.py run <run_id>` | 跑全管道（health → search → merge → enrich → rank → output） |
| `python main.py run <run_id> --from merge --to rank` | 只跑部分阶段 |
| `python main.py run <run_id> --yes` | 非交互模式（决策点自动选默认策略） |
| `python main.py run <run_id> --force` | 强制重跑已完成阶段 |
| `python main.py rollback <run_id> --to <step>` | 回退到某步（删除该步及之后的产物） |
| `python main.py delete <run_id> [--yes]` | 删除任务（整个 run 目录，不可恢复；共享摘要缓存保留） |
| `python main.py health <run_id>` | 健康检查（等价于 scripts/health_check.py） |
| `python main.py report <run_id>` | 查看运行报告 |
| `python scripts/health_check.py [--json] [--db xxx]` | 独立健康检查，退出码 0/1/2 |
| `python launch_browser.py --status` | 浏览器状态 |
| `python launch_browser.py --kill` | 关闭本脚本启动的浏览器（保留登录态） |

## 分步执行与回退（核心设计）

管道固定 6 步，每步一个文件夹，中间量按步归档：

```
memory/output/<run_id>/
├─ search_concept.json     # 任务定义（根）
├─ stage_report.json       # 最后执行记录（根，跨步）
├─ run_report.md           # 运行报告（根，跨步）
├─ step0_health/           # 健康检查（日志）
├─ step1_search/           # 各库搜索结果（付费 4 库 + 免费源 openalex_results.json）
├─ step2_merge/            # merged_results.json + merged_filtered.json
├─ step3_enrich/           # enriched.json + tier2_task/tier2_result.json（Tier 2 中间量）
├─ step4_rank/             # ranked.json + ai_ranked.json + summarized.json + themed.json
└─ step5_output/           # final_results.md + results.bib
```

- **断点续跑**：某步产物齐全即跳过；失败原地停，`--from <step>` 续跑
- **回退**：`rollback --to <step>` 删除该步及之后的 step 文件夹 + 状态文件，从该步重新执行
- **健康检查每次 run 都真实执行**（不跳过）：缺依赖 / 缺 key / 浏览器没启动 / 登录态失效 → 直接报"为什么失败 + 你该做什么"
- 旧版平铺产物任务自动兼容（`stage_done` 新旧双路径检查），可直接断点续跑

## 报错管理设计

**确定性规则 → 代码直接判断：**
- 产物齐全才跳过；失败原地停；每阶段写 `stage_report.json` + 步骤文件夹内 `log.txt`
- 运行报告汇总为 `run_report.md`，失败时下一步建议直接打印

**模糊判断 → 交互问人（`--yes` 用默认策略）：**
- 某库搜索结果偏少（< 30 篇）→ 继续 or 停止调整查询
- 摘要缺失率 > 5% → 跑 Tier 2 补摘要（默认）/ 继续 / 停止

## 退出码

- `0` 成功
- `1` 可重试失败（页面空 / API 限流 / 登录态失效）
- `2` 环境/配置错误（缺依赖 / 缺 key / 浏览器没启动）

## 目录结构

```
main.py                  管道状态机入口（CLI 核心）
webui.py                 本地 Web 控制台（零依赖 http.server + API + 进程管理）
webui/index.html         Web 前端单页（任务列表/步骤条/进度/结果，内嵌 CSS/JS）
launch_browser.py        浏览器启动/复用（独立 profile 持久化登录态，--open 可多 URL，同站点复用标签页）
open-databases.bat       一键启动：浏览器 + 各库主页 + 健康检查（仅 Windows，全英文防编码坑）
.env / .env.example      配置（所有 key 只放 .env：SCOPUS_API_KEY / LLM_API_KEY；免费源不需要）
config.yaml              数据库 URL 等静态配置（随项目分发，无 key）
extractors/*.js          各库页面提取器（列表页 + 详情页）
scripts/health_check.py  统一健康检查（依赖/key/浏览器/登录态，CLI + Web 共用；免费源任务自动跳过 key/浏览器/登录态）
scripts/pipeline/        各阶段脚本（merge/enrich/rank/output/...）
scripts/pipeline/_search_free.py  免费源搜索（OpenAlex，纯 API，无 key 无浏览器）
scripts/utils/           工具（env_config/cdp_utils/encoding/cache_utils/...）
memory/paper-abstracts/  摘要全局缓存（跨任务复用，DOI/渠道主键/标题哈希三层命名）
memory/output/<run_id>/  每次任务的产物（按步骤分文件夹，见上文）
```

## 可选：AI 增强（不配置也能跑）

`ai_rerank.py`（AI 语义评分）/ `ai_summarize.py`（中文摘要）/
`theme_cluster.py`（主题聚类）直接调用 LLM API（默认 DeepSeek），
未配置 `LLM_API_KEY` 时自动跳过，只跑确定性评分（paper_ranker）。
主题聚类无 LLM 时自动降级为单主题。

## 免费源 vs 付费库

| | 免费源（OpenAlex） | 付费库（IEEE / Scopus / ACM / EV） |
|---|---|---|
| 需要 | 无（纯 API） | 机构登录态 + SCOPUS_API_KEY |
| 覆盖 | 综合 2.5 亿+（OpenAlex） | 各库专业索引（EV 工程、IEEE 电子等） |
| 摘要 | 自带（OpenAlex inverted index 重建） | 部分缺 → Tier 2 详情页补 |
| 引文 | OpenAlex 有 | 各库口径（CITATION_SCALE 归一） |
| 适合 | 体验管道流程、CS/EE/物理领域快速预览 | 正式综述、工程领域全覆盖 |

- 免费源结果同样写缓存（有 DOI 走 `10.xxx` 共享缓存，无 DOI 走 `arxiv_xxx` / `openalex_Wxxx` 主键缓存），
  之后跑付费库任务可直接命中
- 健康检查按任务选择自动降级：全免费源任务跳过 key/浏览器/登录态检查

## 已知注意事项

- **免费源覆盖偏科**：OpenAlex 对 CS、EE、物理覆盖好，偏门工程领域结果可能偏少——适合体验管道和快速预览，正式综述建议加付费库
- **登录态会过期**：ACM 是机构 SSO（机构统一认证），会话过期会自动跳登录页；IEEE 机构访问同样可能过期。健康检查会准确报出哪个库失效，去浏览器重登即可（免费源任务不受影响）
- **ACM 偶发 Cloudflare 人机验证**：健康检查偶报，重试或手动过验证即好
- **浏览器/CDP**：搜索阶段需要浏览器在线（端口 18800，`.browser-profile/` 持久化登录态），先跑 `open-databases.bat` 或 `launch_browser.py`
- **单任务并发**：同一时间只跑一个管道（Web 端有锁；CLI 端靠使用者自觉），避免两个任务抢浏览器

## 历史

- 项目源于 OpenClaw 技能（已发布 ClawHub/GitHub），2026-08-06 起脱离 OpenClaw 生态改为 standalone

