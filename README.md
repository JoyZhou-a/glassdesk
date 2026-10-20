# GlassDesk

**玻璃加工企业的英文客服知识助手。** 让客服在 30 秒内拿到一句可以直接对客户说的
英文答案，并附上依据。

---

## 它解决什么问题

一家在澳洲的玻璃厂，设备和技术来自中国和德国，**官方文档全是英文**。
但工程师是中国人，做不了英文电话；客服是英文母语的本地人，读不懂技术手册。

于是每来一个技术问题，就变成这样：

```
客户来电 → 客服听不懂关键点 → 打电话找工程师 → 工程师用中文解释
        → 客服自己翻译成英文 → 回给客户
```

**结果**：一个电话拖十几分钟，工程师一天被打断十几次，客户等不及就走了。

## 它现在怎么做

客服在界面上输入客户的问题，直接得到：

> **Q: What glass do I need for a pool fence?**
>
> Use a laminated toughened assembly for a pool fence. The barrier must stay
> effective if the glass breaks. Never quote ordinary toughened glass for a
> pool barrier.
>
> `(Glazing used as a pool barrier)` `(AS/NZS 2208 - Safety glazing materials)`

答案、出处、耗时都摆在桌面上。**出处是代码按检索结果拼的，不是模型写的**——
模型会编页码，这在合规问题上不能接受。

## 三个刻意的设计选择

**1. 能用查表的，绝不问大模型。**
尺寸和价格答错是赔钱事故。所以"12mm 钢化能做 2400×3000 吗"这类问题，
系统抽出参数直接查表，**1.2 毫秒出答案，不花一分钱 token**。
只有模糊问题才交给模型。

**2. 答不了就说答不了。**
系统有五种终态：能答、需要追问、资料不足、超出能力、服务异常。
**永远不会静默返回一个空答案，也不会在没依据时硬编一个。**

**3. 经验不能冒充规范。**
知识库分三级：标准 / 手册 / 经验。来自工单和老师傅经验的结论，
必须说成 "Usually…" 或 "In our experience…"，不能当成强制要求讲给客户。

## 效果

| 指标 | 数字 |
|---|---|
| 查表类问题（尺寸/价格/交期） | **1.2 ms**，**0 token**，正确率 **100%** |
| 文档类问题（检索 + 生成） | p95 **247 ms**，Recall@5 **88.7%** |
| 答案状态正确率 | **98%**（该答的答、该问的问、该拒的拒） |
| 出处覆盖 | **100%** |
| 测试 | **210 passed** |

## 怎么跑起来

```powershell
git clone https://github.com/JoyZhou-a/glassdesk.git
cd glassdesk
uv sync --extra ml

.\.venv\Scripts\python.exe scripts/check_env.py     # 环境自检（MySQL/Redis/Milvus/模型）
.\.venv\Scripts\python.exe scripts/load_tables.py   # 载入结构化表
.\.venv\Scripts\python.exe scripts/serve.py         # 打开 http://127.0.0.1:8000
```

不想起服务，也可以直接命令行问：

```powershell
.\.venv\Scripts\python.exe scripts/ask.py "There is mist inside my double glazing"
```

> 需求基线见 [`docs/00_PRD.md`](docs/00_PRD.md)，开发规约见 [`CLAUDE.md`](CLAUDE.md)，
> 评测口径与已知局限见 [`docs/13_M6评测体系.md`](docs/13_M6评测体系.md)。

---


## 项目状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架 / 配置 / 日志 / 测试 | ✅ 完成 |
| **M1** | 语料生成规格 + seeds（9 份文档） | ✅ 完成 |
| **M1a** | 建表 / 五类 loader / 分源切分 / 写库（不写向量） | ✅ 完成 |
| **M1b** | bge-m3 编码 + 写入 Milvus | ✅ 完成 |
| M1c | OCR（扫描件） | ⬜ 押后（只影响 1 份文档） |
| **M2** | 检索（hybrid 召回 + 交叉编码器重排 + 父子块还原） | ✅ 完成 |
| **M3** | 生成（电话/邮件双模式 + 代码拼出处 + 拒答 + 磁盘缓存） | ✅ 完成 |
| **M4a/b** | 槽位通道（抽参数 → 查表 → 模板），零 token | ✅ 完成 |
| M4c | 意图分类器（distilbert） | ⬜ 非必需（路由已由槽位判定） |
| **M5** | 前端（FastAPI 接口 + 单页界面，零新依赖） | ✅ 完成 |
| **M6** | 评测体系（三组指标分开报 + 标签修正） | ✅ 完成 |

## 详细指标

上面的"效果"是速览版。这里是三组指标的完整口径——**分开报，不加总**，
因为失败模式不同（数字算错 / 没检索到 / 该拒没拒），加总会互相掩盖。

| 组 | 内容 | 结果 |
|---|---|---|
| 结构化（零 token） | 尺寸 30 + 价格 20 + 交期 10 | **100%**（60/60），11 条负面样例全部安全处理 |
| 向量检索（零 token） | 120 条评测集 | **Recall@5 88.7%** / MRR 0.789；延迟 p50 **172 ms** / p95 **247 ms** |
| 答案生成 | 60 条 | 状态正确 **98%**、引用覆盖 **100%** |

> 检索里 `spec` / `logistics` 两类只有 70%，是**向量检索的原理上限**（表格行只有
> 数字不同，语义空间分不开）。这两类在线上走结构化通道，实测 100%，不再进检索。

一键复现：`.\.venv\Scripts\python.exe scripts/eval_all.py`

口径与已知局限见 [docs/13_M6评测体系.md](docs/13_M6评测体系.md)。

**当前入库结果**：8 份文档 active，363 个 chunk（88 父块 + 275 子块），
其中 275 个子块已编码为向量写入 Milvus（dense 1024 维 + sparse）。
`manual-tempering-scan` 是扫描件，等 M1c 的 OCR。

> 编码跑在 **GPU** 上（torch 2.13.0+cu130，复用机器上已有的 conda 环境）。
> `uv sync --extra ml` 会换回 CPU 版（**不报错，只是慢 140 倍**），
> 之后需重跑 `pwsh scripts/use_conda_torch.ps1`；`scripts/check_env.py` 会检测这种降级。

> 看不懂代码？先读 [`docs/10_代码导读.md`](docs/10_代码导读.md) —— 不用看 Python 也能理解系统怎么运转。

---

## 环境要求

| 项 | 要求 | 本机实测 |
|---|---|---|
| OS | Windows 11 | ✅ |
| Python | **3.12**（不要用 3.13 / 3.14） | 3.12.14（uv 管理） |
| 包管理 | uv ≥ 0.12 | 0.12.10 |
| Node | ≥ 18（仅前端需要） | v24.19.0 |
| GPU | 可选；8 GB 显存够用但吃紧 | RTX 5060 Ti 8 GB |

### GPU 说明（重要）

RTX 5060 Ti 是 Blackwell 架构（sm_120），**必须用 CUDA 12.8 的 PyTorch 轮子**，
PyPI 默认的 Windows 版 `torch` 是 CPU 版，装了也用不上 GPU。
`pyproject.toml` 已配置 `pytorch-cu128` 索引，`uv sync --extra ml` 会自动拉对版本。

显存规划：bge-m3(fp16) ≈ 2.2 GB + reranker(fp16) ≈ 2.2 GB ≈ 4.4 GB。
8 GB 卡上**两个模型不要同时常驻**——入库阶段只加载 embedder，查询阶段再加载 reranker。

---

## 从零安装（首次）

> 上面「怎么跑起来」是**环境已就绪**时的启动路径。
> 这一节是**第一次拿到代码**时的完整步骤。

```bash
# 1. 依赖
uv sync

# 2. 配置：复制模板并填密钥
copy .env.example .env

# 3. 自检（配置 / 模型 / MySQL / Redis / Milvus / LLM）
uv run python scripts/check_env.py

# 4. 测试
uv run pytest -v

# 5. 构建语料（M1）：校验 → 渲染 XLSX / DOCX / PDF / MD
uv run python scripts/build_corpus.py --check   # 只做数字自洽校验
uv run python scripts/build_corpus.py           # 校验 + 全量渲染
```

### 语料构建的额外依赖

`build_corpus.py` 的 PDF 由 DOCX 经 **LibreOffice Kit** 转换而来（不是 reportlab），
需要两个环境变量指向 bundled 运行时：

```powershell
$env:LO_NODE = "<...>\runtime\primary-runtime\dependencies\node\bin\node.exe"
$env:LO_CLI  = "<...>\app.asar.unpacked\dsh\node_modules\@deepseek-ai\libreoffice-kit\lib\cli.js"
```

扫描版 PDF 由 pymupdf 把文本 PDF 栅格化后重新打包（JPEG 压缩），
脚本会**自动断言它不含文本层**——否则 OCR 路径就没被真正验证。

> 注意：LibreOffice 拒绝覆盖已存在文件，脚本已处理（转换前先删），可反复执行。

### 依赖服务

服务跑在 VMware 虚拟机上（`192.168.88.100`，VMnet8 / NAT）：

| 服务 | 端口 | 用户名 | 库 |
|---|---|---|---|
| Milvus 2.4.4 | 19530 | 无认证 | `glassdesk` / `glassdesk_test` |
| MySQL 8.0.43 | 3306 | root | 同上 |
| Redis 7.2.16 | 6379 | 密码 1234 | db=1 / db_test=15 |

---

## 目录结构

```
projectA/
├── CLAUDE.md              # AI 开发规约（每次会话注入）
├── config.yaml            # 非密钥配置（入库）
├── .env                   # 密钥（不入库）
├── .env.example           # 模板（入库）
├── pyproject.toml
├── docs/                  # 设计文档
│   ├── 00_PRD.md
│   ├── 环境与连通性报告.md
│   ├── ProjectA_Legacy_RAG.md
│   └── ProjectB_Agentic_RAG.md
├── src/glassdesk/
│   ├── config.py          # 配置加载（yaml + env，密钥不进 yaml）
│   ├── constants.py       # 枚举：Intent / Authority / AnswerState / OutputMode
│   └── logger.py          # 控制台 + 文件双输出
├── scripts/
│   └── check_env.py       # 环境自检
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_constants.py
│   ├── test_logger.py
│   └── test_services_connectivity.py   # 默认跳过，-m integration 才跑
├── data/                  # 语料（不入库原始文件）
│   ├── raw/
│   ├── processed/
│   └── seeds/
├── logs/
└── outputs/
    ├── eval/
    └── traces/
```

**模型外置**：`E:\models\bge-m3`、`E:\models\bge-reranker-v2-m3`、`E:\models\distilbert-base-uncased`。
模型不进仓库，路径由 `config.yaml` 的 `models.*` 指定。

---

## 常用命令

```bash
uv run pytest -v                     # 单元测试（跳过集成）
uv run pytest -m integration -v      # 连通性测试（需要真实服务）
uv run python scripts/check_env.py   # 环境自检
uv sync --extra ml                   # 需要 torch 时（M2 之后）
```
