# TT-AI (NL2SQL Service)

## 项目简介
本项目是一个基于大模型（LLM）的自然语言转 SQL（NL2SQL）智能服务。它采用分层架构和多 Agent 协作模式，旨在将用户的自然语言问题转化为精确的 SQL 查询，并返回数据结果。系统集成了语义层（Semantic Layer）管理、RAG（检索增强生成）以及全链路监控，适用于复杂的业务数据查询场景。

## 核心功能
*   **智能 NL2SQL**: 理解自然语言意图，结合业务语义自动生成并执行 SQL。
*   **语义层驱动**: 通过配置定义指标、维度和虚拟视图，解耦业务逻辑与底层表结构。
*   **RAG 知识增强**: 内置 QA 知识库和元数据检索，辅助模型理解特定业务术语和逻辑。
*   **多 Agent 协作**: Supervisor 架构协调 SQL 生成、数据处理等不同职能的 Agent。
*   **多模态交互**: 支持 API 服务模式和 CLI 命令行交互模式。
*   **可观测性**: 集成 LangFuse，提供详细的 Agent 思考路径和执行链路追踪。

## 技术栈
*   **编程语言**: Python 3.13+
*   **Web 框架**: FastAPI, Uvicorn
*   **Agent 框架**: LangChain, LangGraph (用于构建有状态的多轮对话图)
*   **LLM 交互**: LangChain-OpenAI
*   **ORM / 数据库**: SQLAlchemy, AsyncPG, Postgres
*   **向量检索**: FAISS (本地), ChromaDB
*   **包管理**: uv
*   **配置管理**: Pydantic Settings, YAML
*   **部署**: Docker

## 核心模块与算法位置

### 1. Agent 编排与核心逻辑
*   **Supervisor (总控)**: [src/nl2sql/supervisor/agent.py](src/nl2sql/supervisor/agent.py)
    *   负责会话管理、意图识别和任务分发。
*   **Semantic SQL Agent (核心 NL2SQL)**: [src/nl2sql/agents/sql_agent/graph.py](src/nl2sql/agents/sql_agent/graph.py)
    *   基于 LangGraph 构建的语义 SQL 生成图。
*   **Agentic RAG (探索与检索)**: [src/nl2sql/agents/sql_agent/agentic_rag.py](src/nl2sql/agents/sql_agent/agentic_rag.py)
    *   在生成 SQL 前，自主决定检索 QA 库或语义定义，获取必要的上下文信息。
*   **SQL Generator (生成器)**: [src/nl2sql/agents/sql_agent/sql_generator.py](src/nl2sql/agents/sql_agent/sql_generator.py)
    *   专注于 SQL 语法的生成与修正。

### 2. 语义层与基础设施
*   **语义配置**: [configs/semantic/ai_views.yaml](configs/semantic/ai_views.yaml)
    *   定义虚拟视图、Join 关系、指标计算公式。
*   **RAG 检索器**: [src/nl2sql/infra/store/qa_rag.py](src/nl2sql/infra/store/qa_rag.py)
    *   管理 QA 知识库的向量索引与检索。
*   **服务入口**: [main.py](main.py)
    *   FastAPI 应用工厂与启动入口。

## 快速开始

### 环境要求
*   Python 3.13+
*   uv (推荐) 或 pip

### 安装依赖
```bash
# 使用 uv (推荐)
uv sync

# 或者使用 pip
pip install -r requirements.txt
```

### 配置
1. 复制环境变量示例文件：
   ```bash
   cp .env.example .env
   ```
2. 编辑 `.env` 文件，配置必要的 `OPENAI_API_KEY` 和数据库连接 `DATABASE_URL`。

### 启动服务
*   **开发模式 (支持热重载)**:
    ```bash
    uv run python main.py dev
    ```
*   **生产模式**:
    ```bash
    uv run python main.py prod
    ```
*   **CLI 命令行模式**:
    ```bash
    uv run python -m src.nl2sql.cli
    ```

## 目录结构说明
```text
├── configs/              # 配置文件
│   └── semantic/         # 语义层定义 (ai_views.yaml, qa.md)
├── src/                  # 源代码
│   ├── core/             # 核心组件 (配置, 鉴权, 监控)
│   └── nl2sql/           # NL2SQL 业务域
│       ├── agents/       # 各类 Agent 实现 (sql_agent, gen_data)
│       ├── supervisor/   # Supervisor 总控逻辑
│       ├── infra/        # 基础设施 (DB, LLM, RAG Store)
│       └── api.py        # API 路由定义
├── docker/               # Docker 部署文件
├── main.py               # 程序入口
└── pyproject.toml        # 项目依赖配置
```
