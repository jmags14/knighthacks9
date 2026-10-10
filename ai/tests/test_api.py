"""API router tests. The agent is mocked, so no Ollama is needed."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai.agent import agent, trace
from ai.agent.api import router


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake_answer(text, channel="chat"):
        calls.append((text, channel))
        tr = trace.Trace(channel, text)
        return {"answer": "Boil water for 1 minute.", "sources": [{"source": "water.md"}],
                "route": "rag", "trace_id": tr.save("Boil water for 1 minute.", "rag"),
                "extra": "must not leak"}

    monkeypatch.setattr(agent, "answer", fake_answer)
    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    c.calls = calls
    return c


def test_ask_returns_contract(client):
    r = client.post("/api/ask", json={"text": "  how do I purify water?  "})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"answer", "sources", "trace_id", "route"}      # no "extra"
    assert body["route"] == "rag" and body["sources"] == [{"source": "water.md"}]
    assert client.calls == [("how do I purify water?", "chat")]         # trimmed, default channel


def test_ask_voice_channel(client):
    client.post("/api/ask", json={"text": "hi", "channel": "voice"})
    assert client.calls[-1][1] == "voice"


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "   "}])
def test_ask_empty_text_is_400(client, payload):
    assert client.post("/api/ask", json=payload).status_code == 400
    assert client.calls == []                                           # agent never called


def test_ask_bad_input_is_422(client):
    assert client.post("/api/ask", json={}).status_code == 422
    assert client.post("/api/ask", json={"text": "x", "channel": "fax"}).status_code == 422


def test_traces_newest_first_and_limit(client):
    for q in ("one", "two", "three"):
        client.post("/api/ask", json={"text": q})
    rows = client.get("/api/traces?limit=2").json()
    assert [r["question"] for r in rows] == ["three", "two"]
    assert isinstance(rows[0]["timings"], dict) and "tools_json" not in rows[0]
    assert client.get("/api/traces?limit=0").status_code == 422
