"""Build the P3 prompt (AGENT_PLAN 5.4). Short on purpose: prompt length is what makes the Pi slow."""
from ai.agent import config, templates, tools
from ai.agent.rag import clean_source

# Small models (1B) copy any rule they see, so the base prompt is tiny and the safety
# rules below are added only when they apply. "No match" is decided by code in
# agent.py (guide similarity), not by the model.
SYSTEM = (
    "You are StormBox, an offline emergency assistant. Do not introduce yourself. "
    "Read the passages, then answer the question in 2-3 short, calm sentences. "
    "Use the facts and numbers from the passages and add nothing else."
)
RULE_ALARM = "An alarm is active. Never contradict it."
RULE_AIR = "The air sensor is not a carbon monoxide detector."
RULE_911 = "If phone service works, call 911 in an emergency."
AIR_WORDS = ("air", "carbon", "monoxide", "fumes", "generator", "smoke", "co ")


def system_prompt(question, state, topic=None):
    rules = [SYSTEM]
    if state.get("alarms"):
        rules.append(RULE_ALARM)
    q = question.lower() + " "
    if topic == "air" or any(w in q for w in AIR_WORDS):
        rules.append(RULE_AIR)
    if topic in ("first-aid", "flood", "air", "heat", "medical"):
        rules.append(RULE_911)
    return " ".join(rules)


def words(text):
    return len(text.split())


def trim(text, max_words):
    """Cut text to max_words, ending at a sentence if that keeps at least half of it."""
    parts = text.split()
    if len(parts) <= max_words:
        return text
    cut = " ".join(parts[:max_words])
    dot = cut.rfind(". ")
    return cut[:dot + 1] if dot > len(cut) // 2 else cut + "..."


def _num(value, unit, note=""):
    """'1015 hPa (sea level)' or 'not measured'. Every number gets a label and a unit,
    because a 1B model will mix up bare numbers (it once called a pressure a humidity)."""
    return "not measured" if value is None else f"{value:.0f} {unit}" + (f" ({note})" if note else "")


def readings_line(state):
    """Labeled live context, e.g.
    'pressure: 1015 hPa (sea level), pressure trend: falling, temperature: 29 C (inside the house),
     humidity: not measured, wind: 5 mph, active alarms: flood, wall power: ON, data: live'."""
    trend = templates.pressure_trend(state.get("storm", {}).get("phase")) or "unknown"
    alarms = ", ".join(a["id"] for a in state.get("alarms", [])) or "none"
    bits = [
        "pressure: " + _num(tools.sea_level_pressure(state), "hPa", "sea level"),
        f"pressure trend: {trend}",
        "temperature: " + _num(state.get("temp_c"), "C", "inside the house"),
        "humidity: " + _num(state.get("humidity"), "%", "inside the house"),
        "wind: " + _num(state.get("wind_mph"), "mph"),
        "floor: " + (tools.floor_state(state) or "not measured"),
        f"active alarms: {alarms}",
        "wall power: " + ("ON" if state.get("wall_power") else "OFF"),
        "data: " + ("REPLAY of a past storm" if state.get("source") == "replay" else "live"),
    ]
    return ", ".join(bits)


def build_prompt(question, state, chunks, topic=None, max_words=config.PROMPT_MAX_WORDS):
    """SYSTEM + READINGS + numbered PASSAGES + QUESTION. Passages are trimmed so the
    whole prompt stays at or under max_words."""
    head = f"{system_prompt(question, state, topic)}\n\nREADINGS: {readings_line(state)}\nPASSAGES:\n"
    tail = f"QUESTION: {question}\nANSWER:"
    labels = sum(words(f"Passage {n} - {c['title']}: {c['heading']} ({clean_source(c['source']).split(';')[0]}): ")
                 for n, c in enumerate(chunks, 1))
    fixed = words(head) + words(tail) + labels
    budget = max(25, (max_words - fixed) // max(1, len(chunks)))
    lines = []
    for n, c in enumerate(chunks, 1):
        # e.g. "Passage 1 - Floods: Water coming into the house (Ready.gov (FEMA) - Floods): ..."
        label = f"Passage {n} - {c['title']}: {c['heading']} ({clean_source(c['source']).split(';')[0]}): "
        lines.append(label + trim(c["text"], budget))
    return head + "\n".join(lines) + "\n" + tail
