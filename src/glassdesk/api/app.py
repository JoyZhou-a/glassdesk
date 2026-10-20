"""FastAPI 应用：接口 + 单页界面。

**只有一份编排逻辑**，这里调的还是 ``glassdesk.answer.flow.run``。
接口层只负责：加载资源、计时、把结果整理成响应模型。

资源（MySQL/Milvus/嵌入模型/重排模型/LLM）在启动时初始化一次并常驻，
因为加载 bge-m3 要几秒，不能每个请求都重来。

可测试性：``create_app(ask_fn=...)`` 可以注入假的问答函数，
测试时完全不碰数据库和模型。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from glassdesk.answer import flow
from glassdesk.answer.generator import AnswerGenerator
from glassdesk.answer.llm import CachedLLM
from glassdesk.api.schemas import AskRequest, AskResponse, HealthResponse, Timing, TokenUsage
from glassdesk.config import load_settings
from glassdesk.constants import OutputMode
from glassdesk.logger import get_logger, setup_logging

log = get_logger("api.app")
STATIC_DIR = Path(__file__).parent / "static"

EXAMPLES = [
    "What glass do I need for a pool fence?",
    "There is mist inside my double glazing",
    "How much per square metre for 12mm toughened glass?",
    "Can you make 12mm toughened glass 2400 wide by 3000 high?",
    "Can you make 19mm toughened glass 3000 wide by 5000 high?",
    "Do you supply glass for submarines?",
]


class LivePipeline:
    """常驻资源容器。启动一次，服务期间复用。"""

    def __init__(self) -> None:
        from glassdesk.retrieval.embedder import BGEEmbedder
        from glassdesk.retrieval.rerank import Reranker
        from glassdesk.retrieval.service import RetrievalService
        from glassdesk.storage import MilvusStore, MySQLStore

        self.settings = load_settings()
        setup_logging(self.settings)
        started = time.perf_counter()

        self.store = MySQLStore(self.settings.services.mysql,
                                database=self.settings.services.mysql.database)
        self.milvus = MilvusStore(self.settings.services.milvus)
        self.embedder = BGEEmbedder(self.settings.models.embedder, batch_size=1)
        self.reranker = Reranker(self.settings.models.reranker)
        self.retriever = RetrievalService(self.settings, self.store, self.milvus,
                                          self.embedder, self.reranker)
        self.llm = CachedLLM(self.settings.llm, self.settings.paths.outputs / "llm_cache")
        self.generator = AnswerGenerator(self.llm, self.settings.answer)

        # 预热：嵌入模型和重排模型都是懒加载，第一次真实请求要等约 5 秒。
        # 演示时那 5 秒很难看，所以启动阶段先跑一次完整检索把它顶掉。
        # 失败不影响服务（比如 Milvus 暂时不可达），真实请求会再报错。
        warm = time.perf_counter()
        try:
            self.retriever.retrieve("warmup")
            log.info("预热完成，耗时 {:.1f}s", time.perf_counter() - warm)
        except Exception as exc:  # noqa: BLE001
            log.warning("预热失败（不影响启动）：{}", exc)

        log.info("服务资源就绪，耗时 {:.1f}s", time.perf_counter() - started)

    def ask(self, question: str, mode: str, use_cache: bool) -> AskResponse:
        self.llm.set_cache_enabled(use_cache)
        before = self.llm.stats.as_dict()
        started = time.perf_counter()

        result = flow.run(
            question,
            store=self.store,
            retriever=self.retriever,
            generator_factory=lambda: self.generator,
            mode=OutputMode(mode),
        )
        total_ms = (time.perf_counter() - started) * 1000

        after = self.llm.stats.as_dict()
        text = result.text if isinstance(result.text, str) else "\n".join(result.text)
        return AskResponse(
            question=question,
            mode=mode,
            route=result.route,
            status=result.status,
            answer=text,
            citations=result.citations,
            authority=result.authority,
            evidence=result.evidence,
            notes=result.notes,
            timing=Timing(retrieval_ms=round(result.retrieval_ms, 1),
                          total_ms=round(total_ms, 1)),
            tokens=TokenUsage(
                calls=after["calls"] - before["calls"],
                cache_hits=after["cache_hits"] - before["cache_hits"],
                prompt_tokens=after["prompt_tokens"] - before["prompt_tokens"],
                completion_tokens=after["completion_tokens"] - before["completion_tokens"],
            ),
        )

    def health(self) -> HealthResponse:
        try:
            row = self.store.fetchone("SELECT v FROM app_meta WHERE k='kb_version'")
            docs = self.store.fetchone(
                "SELECT COUNT(*) AS n FROM documents WHERE status='active'")
            chunks = self.store.fetchone(
                "SELECT COUNT(*) AS n FROM chunks WHERE kind='child' AND status='active'")
            vectors = self.milvus.count()
            return HealthResponse(
                ok=True, model_loaded=True,
                kb_version=row["v"] if row else None,
                documents=int(docs["n"]) if docs else 0,
                chunks=int(chunks["n"]) if chunks else 0,
                vectors=int(vectors),
            )
        except Exception as exc:  # noqa: BLE001
            return HealthResponse(ok=False, model_loaded=True, detail=str(exc))

    def close(self) -> None:
        self.milvus.close()
        self.store.close()


def create_app(ask_fn=None, health_fn=None) -> FastAPI:
    """构建应用。``ask_fn`` / ``health_fn`` 为空时使用真实资源。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if ask_fn is None:
            app.state.pipeline = LivePipeline()
            app.state.ask = app.state.pipeline.ask
            app.state.health = app.state.pipeline.health
        else:
            app.state.pipeline = None
            app.state.ask = ask_fn
            app.state.health = health_fn
        yield
        if app.state.pipeline is not None:
            app.state.pipeline.close()

    app = FastAPI(title="GlassDesk", version="0.8", lifespan=lifespan)

    @app.get("/")
    async def index() -> FileResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=500, detail="页面文件缺失")
        return FileResponse(page)

    @app.get("/api/examples")
    async def examples() -> dict:
        return {"examples": EXAMPLES}

    @app.get("/api/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        if app.state.health is None:
            return HealthResponse(ok=True, model_loaded=False, detail="未初始化资源")
        return app.state.health()

    @app.post("/api/ask", response_model=AskResponse)
    async def ask(payload: AskRequest) -> AskResponse:
        question = payload.question.strip()
        if not question:
            raise HTTPException(status_code=422, detail="问题不能为空")
        try:
            return app.state.ask(question, payload.mode, payload.use_cache)
        except Exception as exc:  # noqa: BLE001
            log.exception("问答失败")
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


#: uvicorn 的入口（``uvicorn glassdesk.api.app:app``）
app = create_app()
