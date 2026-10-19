# GlassDesk

玻璃企业英文客服知识助手 —— 把"打电话问工程师、再自己翻译"这条链路，
换成"30 秒内拿到可直接使用的英文答案 + 出处"。

> 需求基线见 [`docs/00_PRD.md`](docs/00_PRD.md)，开发规约见 [`CLAUDE.md`](CLAUDE.md)。

---

## 项目状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M0** | 骨架 / 配置 / 日志 / 测试 | 🚧 进行中 |
| M1 | 语料生成 + 解析入库 | ⬜ |
| M2 | 检索（hybrid + 父子块 + rerank） | ⬜ |
| M3 | 生成（双模式 + 引用 + 拒答） | ⬜ |
| M4 | 意图 + 结构化槽位通道 | ⬜ |
| M5 | 前端（悬浮窗 / 邮件面板 / 管理页） | ⬜ |
| M6 | 评测与调优 | ⬜ |

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

## 快速开始

```bash
# 1. 依赖
uv sync

# 2. 配置：复制模板并填密钥
copy .env.example .env

# 3. 自检（配置 / 模型 / MySQL / Redis / Milvus / LLM）
uv run python scripts/check_env.py

# 4. 测试
uv run pytest -v
```

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
