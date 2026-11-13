# HelloMinerU

HelloMinerU 是一个用于**验证 MinerU 对各类文档解析能力**的 Python 应用：以 HTTP 接口对外提供解析能力，并内置一个浏览器工作台，用于上传文档、配置解析参数、查看 Markdown 结果、进行人工标注评分与多引擎对比。

- 默认产物为 **Markdown**，同时输出统一 `content_list.json`、页面缩略图与元数据
- 原生数字版文档可走 **PyMuPDF / python-docx / python-pptx / openpyxl** 等轻量解析器，无需 OCR
- MinerU 支持**子进程 CLI** 与**进程内 API** 两种执行方式，全部参数可通过接口配置
- 任务队列基于 **SQLite + HTTP Worker 拉取**，无需 Redis，可单机或分布式部署
- 五维人工评分 + 逐元素标注，结果与评分**持久化在硬盘**（SQLite + JSON 快照）

---

## 1. 功能概览

| 模块 | 说明 |
| --- | --- |
| 解析工作台 | 上传文件 → 自动识别文档类型 → 选择引擎/参数 → 提交任务 → 轮询进度 → 查看 Markdown/版式/内容块/日志/产物 |
| 评测看板 | 五维评分（1–5）、逐元素标注、按引擎/backend/method/runner 分组统计、CSV/JSON 导出、同文档多运行对比 |
| Worker 节点 | 查看在线节点、能力、当前任务、完成/失败数；支持 coordinator / worker 分离部署 |
| HTTP API | 上传、解析、查询、取消、日志增量读取、结果与文件下载、标注、统计、导出 |

### 支持的文档与引擎

| 引擎 | 扩展名 | 特点 |
| --- | --- | --- |
| `auto` | 全部 | 按文件类型和 PDF 文本层自动选择 |
| `mineru` | pdf、图片 | 深度解析，支持 pipeline / vlm / hybrid、OCR、版面、表格、公式 |
| `pymupdf` | pdf | 原生 PDF 文本/表格/图片抽取，速度快、CPU 友好 |
| `docx` | docx | 标题、段落、列表、表格、图片转 Markdown |
| `pptx` | pptx | 按幻灯片转 Markdown |
| `xlsx` | xlsx、xlsm、csv | 每个 sheet / 文件转 Markdown 表格 |

PDF 自动识别规则（抽样最多 5 页）：平均字符数 ≥100 → `pymupdf`；40–100 → `mineru( auto )`；<40 → `mineru( ocr )`。图片固定走 MinerU OCR。

---

## 2. 快速开始

环境要求：Python 3.10+（验证环境为 3.12 / Windows），建议安装完整 MinerU 依赖。

```powershell
cd d:\work\pyspace6\HellominerU
pip install -r requirements.txt

# 启动单机服务（内置 Worker，默认 http://127.0.0.1:8000）
python -m app
```

打开浏览器访问 `http://127.0.0.1:8000` 即可看到工作台。

生成样例文档（可选）：

```powershell
python tools/make_samples.py   # 输出到 data/samples/
```

样例包含：文本型 PDF、表格 PDF、扫描版 PDF、DOCX、PPTX、XLSX、CSV、PNG。

### MinerU 模型准备

首次执行 MinerU 需要下载模型权重：

```powershell
# 方式一：使用 MinerU 官方下载命令（可选 modelscope 加速）
mineru-models-download

# 方式二：首次解析时自动下载（需保持网络可用）
```

相关环境变量：

- `HF_HOME`：HuggingFace 缓存目录（例如 `D:\huggingface`），需要可写
- `MINERU_MODEL_SOURCE`：`huggingface` / `modelscope` / `local`
- `MINERU_DEVICE`：`cpu` / `cuda:0`；`MINERU_VIRTUAL_VRAM_SIZE`：pipeline 显存预算

> `device`、`vram`、`model_source` 在 MinerU 首次加载模型时生效；进程内模式修改后需重启服务。

---

## 3. 配置项（环境变量）

所有配置使用 `HMU_` 前缀，也可写入项目根目录 `.env`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HMU_HOST` | `127.0.0.1` | 监听地址，分布式部署设为 `0.0.0.0` |
| `HMU_PORT` | `8000` | 监听端口 |
| `HMU_ROLE` | `both` | `both`（单机）/ `coordinator`（调度）/ `worker`（解析） |
| `HMU_DATA_DIR` | `./data` | 数据目录（数据库、上传、结果、导出） |
| `HMU_MAX_UPLOAD_MB` | `100` | 单文件上传大小上限 |
| `HMU_MAX_BUNDLE_MB` | `1024` | Worker 结果包大小上限 |
| `HMU_JOB_TIMEOUT_SEC` | `1800` | 任务默认超时 |
| `HMU_DEFAULT_RUNNER` | `subprocess` | MinerU 执行方式默认值 |
| `HMU_MINERU_BACKEND` | `pipeline` | MinerU 默认 backend |
| `HMU_MINERU_METHOD` | `auto` | `auto` / `txt` / `ocr` |
| `HMU_MINERU_LANG` | `ch` | OCR 语言 |
| `HMU_MINERU_DEVICE` | 空（自动） | `cpu` / `cuda:0` 等 |
| `HMU_MINERU_MODEL_SOURCE` | `huggingface` | 模型来源 |
| `HMU_WORKER_TOKEN` | 自动生成 | Worker 鉴权令牌，未配置时写入 `data/worker_token.txt` |
| `HMU_WORKER_NAME` | `主机名-PID` | 节点显示名 |
| `HMU_WORKER_CONCURRENCY` | `1` | 单节点并发任务数 |
| `HMU_COORDINATOR_URL` | 由 host/port 推导 | Worker 连接的协调节点地址 |
| `HMU_CLAIM_POLL_SEC` | `2.0` | Worker 拉取任务间隔 |
| `HMU_HEARTBEAT_SEC` | `10.0` | 心跳间隔 |
| `HMU_LEASE_SEC` | `300` | 任务租约；超时由维护任务回收重排 |
| `HMU_MAX_ATTEMPTS` | `3` | 最大尝试次数 |

---

## 4. 解析参数（POST /api/parse 的 `options`）

`options` 为 JSON 字符串（multipart 字段）：

```json
{
  "engine": "auto",
  "runner": "subprocess",
  "timeout_sec": 1800,
  "mineru": {
    "backend": "pipeline",
    "method": "auto",
    "lang": "ch",
    "server_url": "",
    "start_page_id": 0,
    "end_page_id": null,
    "formula_enable": true,
    "table_enable": true,
    "device": "",
    "virtual_vram": null,
    "model_source": "huggingface"
  },
  "pymupdf": {
    "extract_tables": true,
    "extract_images": true,
    "thumbnail_dpi": 110
  }
}
```

- `engine=auto` 时，服务端根据识别结果将任务落到具体引擎；若推荐 MinerU，还会把 `method=auto` 细化为 `txt` / `ocr`
- `backend` 可选：`pipeline`、`vlm-auto-engine`、`hybrid-auto-engine`、`vlm-http-client`、`hybrid-http-client`
- 页码从 0 开始，`end_page_id` 为闭区间
- `runner=inprocess` 会复用进程内已加载模型，速度更快；`subprocess` 隔离性更好且可实时输出日志

---

## 5. HTTP API

统一前缀 `/api`，请求/响应均为 JSON（上传与结果包除外）。

### 元信息与解析

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务状态、MinerU 版本、设备、队列与 Worker 数 |
| GET | `/parsers` | 引擎列表与参数元数据（前端动态表单来源） |
| POST | `/detect` | multipart `file`，返回文档类型、推荐引擎、页数、平均字符数 |
| POST | `/parse` | multipart `file` + `options`，创建任务，202 返回 `job_id` |
| GET | `/jobs?limit=&status=` | 任务列表（含评分汇总） |
| GET | `/jobs/{job_id}` | 任务详情（状态、阶段、错误、队列位次） |
| GET | `/jobs/{job_id}/result` | 结果：`markdown`、`content_list`、`pages`、`artifacts`、标注 |
| GET | `/jobs/{job_id}/log?offset=&limit=` | 增量读取日志，返回 `lines` 与 `next_offset` |
| GET | `/jobs/{job_id}/files/{path}` | 下载产物（output/ 或 raw/ 下的文件） |
| POST | `/jobs/{job_id}/cancel` | 取消排队/运行任务 |
| DELETE | `/jobs/{job_id}` | 删除任务与磁盘产物（运行中拒绝） |

### 标注与统计

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET / PUT | `/jobs/{job_id}/annotation?annotator=` | 读取/保存五维评分（1–5）与评语 |
| GET / POST | `/jobs/{job_id}/annotation/items?annotator=` | 逐元素标注列表 / 新增 |
| DELETE | `/jobs/{job_id}/annotation/items/{item_id}` | 删除元素标注 |
| GET | `/annotations?engine=&annotator=&limit=` | 评分记录列表 |
| GET | `/stats/scores?group_by=` | 分组统计，如 `engine,backend,method` |
| GET | `/export/annotations?format=csv\|json` | 导出标注，写入 `data/exports/` 并下载 |

### 文档与节点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/documents?limit=` | 文档列表（按内容 SHA 去重，含运行次数/均分） |
| GET | `/documents/{sha256}/runs` | 同一文档的全部解析运行 |
| GET | `/workers` | Worker 列表与队列计数 |

### Worker 协议（需请求头 `X-HMU-Token`）

`POST /workers/register`、`POST /workers/{id}/heartbeat`、`POST /workers/{id}/claim`（无任务返回 204）、
`POST /worker/tasks/{job_id}/progress`、`GET /worker/tasks/{job_id}/file`、
`POST /worker/tasks/{job_id}/result`（multipart `bundle` zip）、`POST /worker/tasks/{job_id}/fail`。

### 调用示例

```powershell
# 识别
curl.exe -X POST -F "file=@data/samples/sample_text.pdf" http://127.0.0.1:8000/api/detect

# 提交解析（options 从文件读取，避免 Windows 下引号转义问题）
'{"engine":"pymupdf"}' | Set-Content -Encoding utf8 data/tmp/opt.json
curl.exe -X POST -F "file=@data/samples/sample_text.pdf" -F "options=<data/tmp/opt.json" `
  http://127.0.0.1:8000/api/parse

# 查询结果
curl.exe http://127.0.0.1:8000/api/jobs/<job_id>/result
```

---

## 6. 分布式部署

默认 `HMU_ROLE=both`：API 与解析在同一进程，内置 Worker 通过 `LocalTransport` 直接消费任务，无自我 HTTP 调用。

分离部署时，协调节点负责 API、数据库、产物存储；解析节点只执行任务并回传结果包：

```powershell
# 协调节点
$env:HMU_ROLE="coordinator"
$env:HMU_HOST="0.0.0.0"
python -m app

# 解析节点（可多台）
$env:HMU_ROLE="worker"
$env:HMU_COORDINATOR_URL="http://<coordinator-ip>:8000"
$env:HMU_WORKER_TOKEN="<data/worker_token.txt 中的令牌>"
$env:HMU_WORKER_CONCURRENCY="1"
python -m app
```

机制说明：

- 任务存放在 SQLite（WAL），Worker 通过 `claim` 原子领取，领取时写入 `lease_expires_at`
- Worker 定期心跳并续租；协调节点每 30 秒回收过期租约，未超过 `max_attempts` 的任务重新入队
- 取消任务时，心跳响应会返回 `canceled`，Worker 停止执行并回报
- 结果以 zip（`output/` + `raw/`）回传，协调节点校验路径后解压落盘

---

## 7. 数据持久化

```
data/
├─ hellomineru.db          # SQLite：documents / jobs / workers / annotations / annotation_items
├─ worker_token.txt        # 自动生成的 Worker 令牌
├─ uploads/<sha2>/<sha>.<ext>   # 内容寻址的原始文件
├─ jobs/<job_id>/
│  ├─ job.json             # 任务快照（参数、状态、结果元数据）
│  ├─ job.log              # 运行日志
│  ├─ annotations/<annotator>.json   # 评分快照
│  ├─ output/result.md     # Markdown 主产物
│  ├─ output/content_list.json       # 统一下游结构
│  ├─ output/meta.json     # 引擎、耗时、统计、告警
│  ├─ output/images/       # 抽取的图片
│  └─ raw/                 # 引擎原始产物（MinerU 原始目录等）
├─ worker/<job_id>/        # Worker 本地工作区（完成后清理）
├─ exports/                # 标注导出文件
└─ samples/                # 样例文档
```

数据库与 JSON 快照双写：即使数据库损坏，`data/jobs/<id>/job.json` 与 `annotations/*.json` 仍可用于恢复与审计。

标注采用固定五维：`text_accuracy`（文本准确）、`reading_order`（阅读顺序/版式）、`table`（表格）、`formula`（公式）、`image`（图片/图表），每项 1–5 分，总分取均值保留两位。

---

## 8. 界面说明

1. **解析工作台**：左栏上传 + 动态参数表单；右侧任务队列与结果面板（六标签：Markdown 预览 / 版式页面 / 内容块 / 人工标注 / 运行日志 / 产物文件）
2. **评测看板**：评分记录、分组统计、CSV/JSON 导出、同一文档不同引擎运行的左右对比
3. **Worker 节点**：服务健康、队列计数、节点能力与心跳，附分离部署命令提示

前端为原生 HTML + JS，由 FastAPI 直接托管，无需构建；Markdown 渲染使用 marked + DOMPurify + KaTeX + highlight.js（CDN）。

---

## 9. 已知限制

- MinerU 首次运行需要联网下载模型；`vlm-*` / `hybrid-*` backend 需要额外 VLM 模型或远程服务
- 进程内模式下 `device` / `vram` / `model_source` 无法热切换，仅首次加载生效
- PyMuPDF 轻量解析依赖 PDF 文本层，扫描件请使用 MinerU OCR
- XLSX 单表最多读取 2000 行 × 200 列，超出部分会截断并在告警中提示
- 分布式模式要求各节点能访问协调节点的 HTTP 端口，并共享同一 `HMU_WORKER_TOKEN`
