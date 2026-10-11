"""Browser voice routes (/api/voice/*) with Whisper and Piper mocked, plus the trace timings they feed.

No models, no Ollama, no microphone needed: we replace the two slow functions (_transcribe, _synthesize)
or the model modules themselves.
"""
import io
import json
import sqlite3
import sys
import types
import wave

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai.agent import agent, trace
from ai.agent import api as agent_api
from ai.voice import api as voice_api
from ai.voice import config as voice_config

MB = 1024 * 1024


def make_wav(seconds, rate=16000):
    """A silent mono 16-bit WAV of the given length (what the browser uploads)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(voice_api.router)
    return TestClient(app)


@pytest.fixture
def models_ready(monkeypatch):
    monkeypatch.setattr(voice_api, "models_installed", lambda: {"stt": True, "tts": True})


@pytest.fixture
def fake_stt(monkeypatch, models_ready):
    """Replace Whisper. Records what the route handed it: path suffix, file bytes, and whether it existed."""
    seen = {}

    def fake(path):
        seen["path"] = path
        seen["suffix"] = path[path.rindex("."):]
        with open(path, "rb") as f:
            seen["bytes"] = f.read()
        return "Is the storm over?", 812.5
    monkeypatch.setattr(voice_api, "_transcribe", fake)
    return seen


# ================================================================= status
def test_status_reports_ready_models(client, models_ready):
    j = client.get("/api/voice/status").json()
    assert j["stt_ready"] and j["tts_ready"] and j["offline"] is True
    assert j["max_seconds"] == 15
    assert "faster-whisper" in j["stt_model"] and "Piper" in j["tts_voice"]


def test_status_reports_missing_models(client, monkeypatch, tmp_path):
    monkeypatch.setattr(voice_config, "STT_DIR", tmp_path / "nope")
    monkeypatch.setattr(voice_config, "PIPER_ONNX", tmp_path / "nope.onnx")
    j = client.get("/api/voice/status").json()
    assert j["stt_ready"] is False and j["tts_ready"] is False


# ================================================================= speech to text
def test_stt_wav_upload_returns_text_and_timing(client, fake_stt):
    wav = make_wav(2)
    r = client.post("/api/voice/stt", content=wav, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 200
    j = r.json()
    assert j["text"] == "Is the storm over?" and j["stt_ms"] == 812.5
    assert j["audio_seconds"] == pytest.approx(2.0) and j["offline"] is True
    assert j["engine"].startswith("faster-whisper")
    assert fake_stt["suffix"] == ".wav" and fake_stt["bytes"] == wav     # the exact upload reached Whisper


def test_stt_non_wav_upload_is_saved_as_webm_so_pyav_can_decode_it(client, fake_stt):
    r = client.post("/api/voice/stt", content=b"\x1aE\xdf\xa3 not really webm", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 200 and fake_stt["suffix"] == ".webm"
    assert r.json()["audio_seconds"] is None                              # length unknown for non-WAV


def test_stt_temp_file_is_deleted_afterwards(client, fake_stt):
    import os
    client.post("/api/voice/stt", content=make_wav(1), headers={"Content-Type": "audio/wav"})
    assert not os.path.exists(fake_stt["path"])


def test_stt_temp_file_is_deleted_even_when_whisper_crashes(client, models_ready, monkeypatch):
    import os
    paths = []

    def crash(path):
        paths.append(path)
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(voice_api, "_transcribe", crash)
    r = client.post("/api/voice/stt", content=make_wav(1), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["detail"] and "secret internal detail" not in r.json()["detail"]
    assert not os.path.exists(paths[0])


def test_stt_empty_upload_is_400(client, fake_stt):
    r = client.post("/api/voice/stt", content=b"", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 400 and "No audio" in r.json()["detail"]


def test_stt_upload_over_5mb_is_413(client, fake_stt):
    r = client.post("/api/voice/stt", content=b"x" * (5 * MB + 1), headers={"Content-Type": "audio/webm"})
    assert r.status_code == 413 and "5 MB" in r.json()["detail"]
    assert "path" not in fake_stt                                         # Whisper was never called


def test_stt_upload_just_under_5mb_is_accepted(client, fake_stt):
    r = client.post("/api/voice/stt", content=b"x" * (5 * MB), headers={"Content-Type": "audio/webm"})
    assert r.status_code == 200


def test_stt_wav_longer_than_15_seconds_is_413(client, fake_stt):
    r = client.post("/api/voice/stt", content=make_wav(16), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 413 and "15 seconds" in r.json()["detail"]
    assert "path" not in fake_stt


def test_stt_wav_of_exactly_15_seconds_is_accepted(client, fake_stt):
    assert client.post("/api/voice/stt", content=make_wav(15), headers={"Content-Type": "audio/wav"}).status_code == 200


def test_stt_damaged_wav_is_400(client, fake_stt):
    r = client.post("/api/voice/stt", content=b"RIFFxxxxnotawav", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 400 and "damaged" in r.json()["detail"]


def test_stt_without_models_is_503_with_the_fix(client, monkeypatch):
    monkeypatch.setattr(voice_api, "models_installed", lambda: {"stt": False, "tts": True})
    r = client.post("/api/voice/stt", content=make_wav(1), headers={"Content-Type": "audio/wav"})
    assert r.status_code == 503 and "download_voice_models.py" in r.json()["detail"]


def test_transcribe_helper_calls_whisper_and_times_it(monkeypatch):
    fake = types.SimpleNamespace(transcribe=lambda path: "hello there")
    monkeypatch.setitem(sys.modules, "ai.voice.stt", fake)
    import ai.voice
    monkeypatch.setattr(ai.voice, "stt", fake, raising=False)
    text, ms = voice_api._transcribe("whatever.wav")
    assert text == "hello there" and ms >= 0


# ================================================================= text to speech
@pytest.fixture
def fake_tts(monkeypatch, models_ready):
    seen = {}

    def fake(text):
        seen["text"] = text
        return make_wav(1), 431.0
    monkeypatch.setattr(voice_api, "_synthesize", fake)
    return seen


def test_tts_returns_wav_audio(client, fake_tts):
    r = client.post("/api/voice/tts", json={"text": "  The storm is far away.  "})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    assert fake_tts["text"] == "The storm is far away."                   # trimmed, otherwise untouched (Piper cleans it)


def test_tts_saves_speech_time_in_the_trace(client, fake_tts):
    tr = trace.Trace("chat", "q")
    trace_id = tr.save("answer", "canned")
    client.post("/api/voice/tts", json={"text": "answer", "trace_id": trace_id})
    assert json.loads(trace.get(trace_id)["timings_json"])["tts"] == 431.0


def test_tts_unknown_trace_id_is_ignored(client, fake_tts):
    assert client.post("/api/voice/tts", json={"text": "hi", "trace_id": 99999}).status_code == 200


def test_tts_empty_text_is_400(client, fake_tts):
    assert client.post("/api/voice/tts", json={"text": "   "}).status_code == 400


def test_tts_text_over_2000_chars_is_413(client, fake_tts):
    r = client.post("/api/voice/tts", json={"text": "a" * 2001})
    assert r.status_code == 413 and "2000" in r.json()["detail"]
    assert client.post("/api/voice/tts", json={"text": "a" * 2000}).status_code == 200


def test_tts_missing_text_field_is_422(client, fake_tts):
    assert client.post("/api/voice/tts", json={}).status_code == 422


def test_tts_without_models_is_503(client, monkeypatch):
    monkeypatch.setattr(voice_api, "models_installed", lambda: {"stt": True, "tts": False})
    assert client.post("/api/voice/tts", json={"text": "hi"}).status_code == 503


def test_tts_crash_is_a_friendly_500(client, models_ready, monkeypatch):
    def crash(text):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(voice_api, "_synthesize", crash)
    r = client.post("/api/voice/tts", json={"text": "hi"})
    assert r.status_code == 500 and "secret internal detail" not in r.json()["detail"]


def test_synthesize_helper_speaks_reads_the_wav_and_deletes_the_file(monkeypatch, tmp_path):
    import os
    wav_path = tmp_path / "out.wav"
    wav_path.write_bytes(make_wav(1))
    spoken = []
    fake = types.SimpleNamespace(speak=lambda text: spoken.append(text) or str(wav_path))
    monkeypatch.setitem(sys.modules, "ai.voice.tts", fake)
    import ai.voice
    monkeypatch.setattr(ai.voice, "tts", fake, raising=False)
    wav, ms = voice_api._synthesize("Pressure is 996 hPa.")
    assert wav[:4] == b"RIFF" and ms >= 0 and spoken == ["Pressure is 996 hPa."]
    assert not os.path.exists(wav_path)


def test_warm_up_loads_installed_models_and_never_raises(monkeypatch):
    loaded = []
    monkeypatch.setitem(sys.modules, "ai.voice.stt", types.SimpleNamespace(_load=lambda: loaded.append("stt")))
    monkeypatch.setitem(sys.modules, "ai.voice.tts", types.SimpleNamespace(_load=lambda: loaded.append("tts")))
    import ai.voice
    monkeypatch.setattr(ai.voice, "stt", sys.modules["ai.voice.stt"], raising=False)
    monkeypatch.setattr(ai.voice, "tts", sys.modules["ai.voice.tts"], raising=False)
    monkeypatch.setattr(voice_api, "models_installed", lambda: {"stt": True, "tts": True})
    voice_api.warm_up()
    assert loaded == ["stt", "tts"]
    monkeypatch.setattr(voice_api, "models_installed", lambda: {"stt": False, "tts": False})
    loaded.clear()
    voice_api.warm_up()
    assert loaded == []                                                   # nothing installed: nothing loaded, no error


# ================================================================= trace timings (stt / agent / tts)
def test_add_timings_updates_memory_copy():
    tr = trace.Trace("voice", "q")
    trace_id = tr.save("a", "canned")
    assert trace.add_timings(trace_id, {"stt": 500.0, "tts": 300.0}) is True
    t = json.loads(trace.get(trace_id)["timings_json"])
    assert t["stt"] == 500.0 and t["tts"] == 300.0 and "agent" in t       # earlier timings are kept
    assert trace.recent(1)[0]["timings"]["tts"] == 300.0                  # what /api/traces returns


def test_add_timings_unknown_trace_returns_false():
    assert trace.add_timings(123456, {"tts": 1.0}) is False


def test_add_timings_updates_the_sqlite_row_too(tmp_path):
    db = tmp_path / "box.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE agent_traces (id INTEGER PRIMARY KEY, ts TEXT, channel TEXT, question TEXT,
                   route TEXT, tools_json TEXT, chunks_json TEXT, sensors_json TEXT, answer TEXT, timings_json TEXT)""")
    con.commit()
    con.close()
    trace.configure(db)
    trace_id = trace.Trace("voice", "q").save("a", "tool")
    assert trace.add_timings(trace_id, {"tts": 250.0})
    stored = json.loads(sqlite3.connect(db).execute("SELECT timings_json FROM agent_traces").fetchone()[0])
    assert stored["tts"] == 250.0 and "agent" in stored


def test_every_trace_records_the_agents_own_time():
    tr = trace.Trace("chat", "q")
    trace_id = tr.save("a", "tool")
    assert json.loads(trace.get(trace_id)["timings_json"])["agent"] >= 0


def test_agent_answer_merges_stt_time_into_the_trace(box, monkeypatch):
    box()
    r = agent.answer("How much battery is left?", "voice", {"stt": 640.2})
    timings = json.loads(trace.get(r["trace_id"])["timings_json"])
    assert timings["stt"] == 640.2 and "agent" in timings
    assert trace.get(r["trace_id"])["channel"] == "voice"


# ================================================================= /api/ask accepts stt_ms
@pytest.fixture
def ask_client(monkeypatch):
    calls = []

    def fake_answer(*args):
        calls.append(args)
        return {"answer": "ok", "sources": [], "trace_id": 1, "route": "tool"}
    monkeypatch.setattr(agent, "answer", fake_answer)
    app = FastAPI()
    app.include_router(agent_api.router)
    return TestClient(app), calls


def test_ask_with_stt_ms_passes_it_to_the_agent_as_timings(ask_client):
    c, calls = ask_client
    r = c.post("/api/ask", json={"text": "Is the storm over?", "channel": "voice", "stt_ms": 812.5})
    assert r.status_code == 200
    assert calls == [("Is the storm over?", "voice", {"stt": 812.5})]


def test_ask_without_stt_ms_keeps_the_two_argument_call(ask_client):
    c, calls = ask_client
    c.post("/api/ask", json={"text": "hello"})
    assert calls == [("hello", "chat")]


def test_ask_rejects_negative_stt_ms(ask_client):
    c, calls = ask_client
    assert c.post("/api/ask", json={"text": "hi", "channel": "voice", "stt_ms": -5}).status_code == 422
    assert calls == []
