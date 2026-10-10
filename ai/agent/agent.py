"""The StormBox agent (AGENT_PLAN 4.2).

answer(text, channel)  -> {"answer","sources","route","trace_id"}   route: tool | canned | rag
explain_event(event)   -> {"answer","lcd","route":"proactive","trace_id"}

Order of work for a question:
  1. router: live-data question (storm now, water days, battery, ...)?  -> P2 template, no LLM
  2. canned: close match to a pre-written answer?                       -> P1, no LLM
  3. otherwise guides + LLM                                             -> P3 (the only LLM path)
Why router before canned: canned answers must not depend on live readings (AGENT_PLAN 5.2),
and "is the storm over?" is close enough to a canned question to steal the match.
"""
import re

import ollama

from ai.agent import canned, config, prompt, rag, router, templates, tools
from ai.agent.trace import Trace

OFF_TOPIC = "I can only help with storm and emergency questions."
NO_SEARCH = ("I can't search the guides right now. If this is an emergency and your phone "
             "works, call 911.")


# ---------------------------------------------------------------- LLM call + fallback (AGENT_PLAN 9)
def _generate(model, text):
    """One Ollama call. Raises if the model is missing, the server is down, or it times out."""
    client = ollama.Client(timeout=config.LLM_TIMEOUT_S)
    extra = {"think": False} if model.startswith("qwen3") else {}      # Qwen3: no hidden reasoning
    r = client.generate(model=model, prompt=text, keep_alive=config.LLM_KEEP_ALIVE,
                        options={"num_predict": config.LLM_NUM_PREDICT,
                                 "temperature": config.LLM_TEMPERATURE}, **extra)
    out = re.sub(r"<think>.*?</think>", "", r["response"], flags=re.S)   # in case a model still thinks
    return out.strip()


def _ask_llm(text, tr):
    """Primary model, then fallback. Returns the answer text or None if both fail."""
    for model in (config.LLM_MODEL, config.LLM_FALLBACK):
        try:
            with tr.step(f"llm:{model}"):
                out = _generate(model, text)
            if out:
                tr.model = model
                return out
            tr.notes.append(f"{model} returned nothing")
        except Exception as e:                  # missing model, timeout, server down
            tr.notes.append(f"{model} failed: {type(e).__name__}")
    return None


# ---------------------------------------------------------------- helpers
def _finish(tr, text, route, sources=None):
    return {"answer": text, "sources": sources or [], "route": route, "trace_id": tr.save(text, route)}


def _sensor_summary(state):
    return {k: state.get(k) for k in ("source", "pressure_hpa", "temp_c", "water", "wall_power")} | {
        "alarms": [a["id"] for a in state.get("alarms", [])], "phase": state["storm"].get("phase")}


def _tool_answer(intent, results, state):
    """P2: turn tool results into a sentence. None = nothing to say (fall through to canned/RAG)."""
    source = state.get("source", "live")
    if intent == "storm_status":
        return templates.storm_status_text(results["get_storm_status"], results["get_alarms"]["alarms"], source)
    if intent == "storm_risk":
        return templates.risk_text(results["get_storm_status"], source)
    if intent == "water_days":
        return templates.water_text(results["water_calculator"])
    if intent == "battery":
        return templates.battery_text(results["get_battery"])
    if intent == "outage":
        return templates.outage_text(results["get_outages"])
    if intent == "sensors":
        return templates.sensors_text(results["get_sensors"], source)
    if intent == "flood_check":
        # Flood alarm ON -> the same words as the proactive alarm. Otherwise fall through.
        if any(a["id"] == "flood" for a in results["get_alarms"]["alarms"]):
            return templates.p0({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"}, source)["say"]
    return None


def warm_up():
    """Call once at startup: embed the canned questions and load the LLM into memory,
    so the first real question is not slow. Safe to skip; failures are ignored."""
    try:
        canned.load()
        _generate(config.LLM_MODEL, "Say OK.")
    except Exception:
        pass


# ---------------------------------------------------------------- entry point 1: questions
def answer(text, channel="chat"):
    tr = Trace(channel, text)
    state = tools.get_state()
    tr.sensors = _sensor_summary(state)
    text = (text or "").strip()
    if not text:
        return _finish(tr, "I didn't catch that. Please ask again.", "tool")

    # --- route (keywords, no LLM) ---
    with tr.step("route"):
        r = router.route(text)
    tr.intent = r["intent"]

    # --- P2: live-data question, answered with a template ---
    with tr.step("tools"):
        results = {name: tools.NO_ARG_TOOLS[name]() for name in r["tools"]}
    tr.tools = results
    if r["path"] == "tool":
        reply = _tool_answer(r["intent"], results, state)
        if reply:
            return _finish(tr, reply, "tool")

    # --- P1: canned answer ---
    try:
        with tr.step("canned"):
            b = canned.best(text)
        tr.canned_score = round(b["score"], 3)
        if b["score"] >= config.CANNED_THRESHOLD and b["score"] - b["runner_up"] >= config.CANNED_MARGIN:
            src = [{"title": b["source"], "source": b["source"], "url": ""}]
            return _finish(tr, b["answer"], "canned", src)
    except Exception as e:                      # Ollama down: skip canned, RAG will report it
        tr.notes.append(f"canned skipped: {type(e).__name__}")

    # --- P3: guides + LLM ---
    try:
        with tr.step("rag"):
            chunks = rag.search(text, r["topic"], config.RAG_K)
    except Exception as e:
        tr.notes.append(f"rag failed: {type(e).__name__}")
        return _finish(tr, NO_SEARCH, "rag")
    tr.chunks = [{"id": c["id"], "title": c["title"], "score": round(c["score"], 3)} for c in chunks]

    top = chunks[0]["score"] if chunks else 0
    if top < config.RAG_MIN_SIM:                # nothing close: off-topic, skip the LLM
        return _finish(tr, OFF_TOPIC, "rag")
    if top < config.RAG_UNSURE:                 # weak match: say so instead of letting a 1B model guess
        c = chunks[0]
        return _finish(tr, f"I'm not sure. The closest guide is: {c['title']}.", "rag", rag.sources_of(chunks[:1]))

    with tr.step("prompt"):
        full_prompt = prompt.build_prompt(text, state, chunks, r["topic"])
    reply = _ask_llm(full_prompt, tr)
    if reply is None:                           # both models failed: show the best guide passage
        reply = "Here's what the guide says: " + chunks[0]["text"]
        return _finish(tr, reply, "rag", rag.sources_of(chunks[:1]))
    return _finish(tr, reply, "rag", rag.sources_of(chunks))


# ---------------------------------------------------------------- entry point 2: events
def explain_event(event):
    """Proactive message for an alarm / phase change / power loss. Templates only, never the LLM.
    An empty answer means 'nothing to say' (alarm cleared, or a phase with no P0 text)."""
    tr = Trace("proactive", str(event))
    state = tools.get_state()
    tr.sensors = _sensor_summary(state)
    source = event.get("source", state.get("source", "live"))
    with tr.step("template"):
        p0 = templates.p0(event, source)
    if p0 is None:
        return {"answer": "", "lcd": ("", ""), "route": "proactive", "trace_id": tr.save("", "proactive")}
    return {"answer": p0["say"], "lcd": p0["lcd"], "route": "proactive",
            "trace_id": tr.save(p0["say"], "proactive")}
