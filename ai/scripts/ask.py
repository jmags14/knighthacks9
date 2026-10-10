"""Terminal chat with the StormBox agent against a FAKE box state.

    python ai/scripts/ask.py                       # interactive
    python ai/scripts/ask.py --state storm         # start with a different fake state
    python ai/scripts/ask.py -q "Is the storm over?" -q "How do I purify water?"   # one-shot

After every answer it prints how the agent got there: route, tools, guide chunks,
sources, canned score, and timings per step.

Commands inside the chat:
    /state calm|storm|flood|eye|outage|replay|hot  switch the fake box state
    /event flood|heat|storm|severe|wind|windalarm|eye|recovering|power   fire a proactive message
    /show                                        print the current fake state
    /quit
"""
import argparse
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ai.agent import agent, tools, trace


# ---------------------------------------------------------------- fake box states (AGENT_PLAN 4.1 shape)
def make_state(name):
    base = {
        "ts": datetime.now(timezone.utc).isoformat(), "source": "live",
        "pressure_hpa": 1015.0, "temp_c": 29.0, "humidity": 70, "water": 20,
        "wind_mph": 6, "rain_in": 0.0, "wall_power": 1, "alarms": [],
        "storm": {"phase": "steady", "risk": 0.02, "alert": False,
                  "reason": "Storm risk 2% because pressure now is 1015.0 hPa (above 1012.7).",
                  "model_version": "storm-risk-20261009"},
        "battery": {"hours_left": 18.0},
        "household": {"people": 3, "pets": 1}, "supplies": {"water_gallons": 6},
        "outages": {"last_lost": None, "last_restored": None},
    }
    if name == "storm":
        base.update(pressure_hpa=996.0, wind_mph=45,
                    alarms=[{"id": "storm", "level": "warn", "since": base["ts"]}])
        base["storm"].update(phase="falling", risk=0.98, alert=True,
                             reason="Storm risk 98% because pressure now is 996.0 hPa (at or below 1007.6).")
    elif name == "flood":
        base.update(water=640, alarms=[{"id": "flood", "level": "alarm", "since": base["ts"]}])
    elif name == "hot":         # AC off: 92 F inside at 70% humidity
        base.update(temp_c=33.3, humidity=70, wall_power=0)
    elif name == "eye":
        base.update(source="replay", pressure_hpa=975.0, wind_mph=8)
        base["storm"].update(phase="possible_eye", risk=0.98, alert=True,
                             reason="Storm risk 98% because pressure now is 975.0 hPa (at or below 1007.6).")
    elif name == "outage":
        lost = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=15)).isoformat()
        base.update(wall_power=0)
        base["outages"]["last_lost"] = lost
        base["battery"]["hours_left"] = 11.5
    elif name == "replay":
        base.update(source="replay", pressure_hpa=1004.0)
        base["storm"].update(phase="falling", risk=0.86, alert=True,
                             reason="Storm risk 86% because pressure now is 1004.0 hPa (at or below 1007.6).")
    elif name != "calm":
        raise ValueError(f"unknown state '{name}'")
    return base


EVENTS = {
    "flood": {"t": "alarm", "id": "flood", "level": "alarm", "state": "on"},
    "heat": {"t": "alarm", "id": "heat", "level": "warn", "state": "on"},
    "storm": {"t": "alarm", "id": "storm", "level": "warn", "state": "on"},
    "severe": {"t": "alarm", "id": "storm", "level": "alarm", "state": "on"},
    "wind": {"t": "alarm", "id": "wind", "level": "warn", "state": "on"},
    "windalarm": {"t": "alarm", "id": "wind", "level": "alarm", "state": "on"},
    "eye": {"t": "phase", "phase": "possible_eye"},
    "recovering": {"t": "phase", "phase": "recovering"},
    "power": {"t": "power", "state": "lost"},
}

current = {"state": make_state("calm")}


# ---------------------------------------------------------------- printing
def show_trace(trace_id):
    t = trace.get(trace_id)["_trace"]
    print(f"  route    : {t.route}   (intent: {t.intent}, canned score: {t.canned_score}, model: {t.model})")
    print(f"  tools    : {', '.join(t.tools) or '-'}")
    print("  chunks   : " + (", ".join(f"{c['id']} ({c['score']})" for c in t.chunks) or "-"))
    print(f"  timings  : {json.dumps(t.timings)}")
    if t.notes:
        print(f"  notes    : {'; '.join(t.notes)}")


def ask(text):
    r = agent.answer(text, channel="chat")
    print(f"\nSTORMBOX: {r['answer']}")
    for s in r["sources"]:
        print(f"  source   : {s['source']}")
    show_trace(r["trace_id"])
    print()


def fire(name):
    ev = EVENTS[name]
    r = agent.explain_event(ev)
    print(f"\nEVENT {ev}\n  say : {r['answer'] or '(nothing to say)'}\n  LCD : {r['lcd'][0]!r} / {r['lcd'][1]!r}")
    show_trace(r["trace_id"])
    print()


def handle_command(line):
    cmd, *rest = line.split(maxsplit=1)
    arg = rest[0].strip() if rest else ""
    if cmd == "/state":
        current["state"] = make_state(arg or "calm")
        print(f"  state is now '{arg or 'calm'}'")
    elif cmd == "/event":
        fire(arg) if arg in EVENTS else print("  events: " + ", ".join(EVENTS))
    elif cmd == "/show":
        print(json.dumps(tools.get_state(), indent=2, default=str))
    elif cmd in ("/quit", "/exit"):
        raise SystemExit
    else:
        print(__doc__)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="calm", help="calm|storm|flood|eye|outage|replay|hot")
    ap.add_argument("-q", action="append", help="ask this question and exit (repeatable)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    current["state"] = make_state(args.state)
    tools.set_provider(lambda: copy.deepcopy(current["state"]))     # tools read this fake box
    agent.warm_up()            # embed canned questions + load the LLM so timings below are realistic
    if args.q:
        for q in args.q:
            print(f"YOU: {q}")
            ask(q)
        return

    print(f"StormBox ask (fake state: {args.state}). Type a question, or /help.")
    while True:
        try:
            line = input("YOU: ").strip()
        except EOFError:
            break
        if not line:
            continue
        handle_command(line) if line.startswith("/") else ask(line)


if __name__ == "__main__":
    main()
