"""API 测试：全部注入假实现，不连数据库、不加载模型。"""

from __future__ import annotations

import pytest

pytest.importorskip("httpx", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient  # noqa: E402

from glassdesk.api.app import create_app  # noqa: E402
from glassdesk.api.schemas import AskResponse, Timing, TokenUsage  # noqa: E402


def fake_ask(question: str, mode: str, use_cache: bool) -> AskResponse:
    return AskResponse(
        question=question, mode=mode, route="documents", status="answer",
        answer="Use laminated toughened glass for a pool fence.",
        citations=["(Glazing used as a pool barrier)"],
        authority="standard", evidence=5,
        timing=Timing(retrieval_ms=12.5, total_ms=340.0),
        tokens=TokenUsage(calls=1, prompt_tokens=900, completion_tokens=30),
    )


@pytest.fixture
def client():
    app = create_app(ask_fn=fake_ask)
    with TestClient(app) as c:
        yield c


def test_index_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "GlassDesk" in response.text


def test_examples_endpoint(client):
    data = client.get("/api/examples").json()
    assert data["examples"]
    assert any("pool fence" in e for e in data["examples"])


def test_ask_returns_answer(client):
    response = client.post("/api/ask", json={"question": "What glass for a pool fence?"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answer"
    assert body["citations"]
    assert body["timing"]["total_ms"] == 340.0
    assert body["route"] == "documents"


def test_ask_passes_mode_through(client):
    body = client.post("/api/ask", json={"question": "q", "mode": "email"}).json()
    assert body["mode"] == "email"


def test_ask_rejects_blank_question(client):
    assert client.post("/api/ask", json={"question": "   "}).status_code == 422


def test_ask_rejects_too_long_question(client):
    assert client.post("/api/ask", json={"question": "x" * 1001}).status_code == 422


def test_ask_rejects_unknown_mode(client):
    assert client.post("/api/ask", json={"question": "q", "mode": "fax"}).status_code == 422


def test_health_without_resources(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["model_loaded"] is False


def test_ask_failure_returns_500():
    def boom(question, mode, use_cache):
        raise RuntimeError("milvus down")

    app = create_app(ask_fn=boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.post("/api/ask", json={"question": "q"})
    assert response.status_code == 500


def test_health_reports_counts_when_injected():
    app = create_app(
        ask_fn=fake_ask,
        health_fn=lambda: __import__(
            "glassdesk.api.schemas", fromlist=["HealthResponse"]
        ).HealthResponse(ok=True, model_loaded=True, kb_version="17",
                         documents=8, chunks=275, vectors=275),
    )
    with TestClient(app) as c:
        body = c.get("/api/health").json()
    assert body["chunks"] == 275 and body["kb_version"] == "17"
