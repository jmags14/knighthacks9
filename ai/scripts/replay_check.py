"""Play a recorded storm through the REAL storm models, then (optionally) chat with the agent at any
moment of the storm. No hardware needed.

    python ai/scripts/replay_check.py                         # table: Hurricane Ian, one line per simulated hour
    python ai/scripts/replay_check.py --file helene_2024_42036.csv
    python ai/scripts/replay_check.py --pressure-only         # same storm, original v1 pressure model only
    python ai/scripts/replay_check.py --at 30 -q "Is the storm over?" -q "What is the weather outside?"
    python ai/scripts/replay_check.py --at 30                 # chat with the agent at hour 30 of the storm

Every line shows which model ran (both / wind / pressure), the risk score and whether it is an alert.
The first 7 simulated hours say "warming up": that is the model collecting its 7 hours of history.
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ask import make_state                      # the fake box (we override its readings below)

from ai.agent import agent, config, tools
from ai.ml.storm_job import run_storm_job

ROWS_PER_HOUR = 6                               # replay files have one row per 10 minutes


def load(name):
    """Replay CSV -> rows. Wind columns are optional (old v1 replays have pressure only)."""
    path = ROOT / "ai" / "ml" / "v2" / "replay" / name
    if not path.exists():
        path = ROOT / "ai" / "ml" / "replay" / name
    rows = []
    for r in csv.DictReader(open(path, newline="")):
        row = {"ts": r["ts"], "pressure_hpa": float(r["pressure_hpa"])}
        if r.get("wind_ms"):
            row.update(wind_ms=float(r["wind_ms"]), gust_ms=float(r["gust_ms"]))
        rows.append(row)
    return rows


def storm_at(rows, i):
    """Run the storm job on everything played so far (rows[0..i])."""
    return run_storm_job(rows[:i + 1], source="replay")


def crude_phase(res):
    """STAND-IN for CS 1's storm-phase rules, only so the agent has a phase to talk about in this test."""
    d3 = (res.get("features") or {}).get("d3")
    if d3 is None:
        return None
    return "falling" if d3 <= -1.0 else "recovering" if d3 >= 1.0 else "steady"


def table(rows):
    print(f"{'simulated time':<22}{'pressure':>9}{'wind m/s':>9}  {'model':<9}{'risk':>6}  alert")
    for i in range(0, len(rows), ROWS_PER_HOUR):
        res = storm_at(rows, i)
        risk = "warming" if res["risk"] is None else f"{res['risk']:.2f}"
        wind = f"{rows[i]['wind_ms']:.1f}" if "wind_ms" in rows[i] else "-"
        print(f"{rows[i]['ts']:<22}{rows[i]['pressure_hpa']:>9.1f}{wind:>9}  {str(res['model']):<9}{risk:>6}  "
              f"{'ALERT' if res['alert'] else ''}")


def box_state_at(rows, i):
    """A fake box whose storm numbers come from the real model at this moment of the replay."""
    res, row = storm_at(rows, i), rows[i]
    state = make_state("calm")
    state.update(source="replay", pressure_hpa=row["pressure_hpa"],
                 wind_mph=row.get("wind_ms", 0.0) * 2.23694)
    state["storm"].update(phase=crude_phase(res), risk=res["risk"], alert=res["alert"],
                          reason=res["reason"], model_version=res["model_version"])
    return state, res


def chat(rows, hour, questions):
    i = min(hour * ROWS_PER_HOUR, len(rows) - 1)
    state, res = box_state_at(rows, i)
    tools.set_provider(lambda: state)
    print(f"\nAt {rows[i]['ts']}: model={res['model']}, risk={res['risk']}, status={res['status']}")
    print(f"  reason: {res['reason']}\n")
    agent.warm_up()
    for q in questions or iter(lambda: input("YOU: ").strip(), "/quit"):
        if q:
            print(f"STORMBOX: {agent.answer(q)['answer']}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default="ian_2022_41009.csv")
    ap.add_argument("--pressure-only", action="store_true", help="use the original v1 pressure model only")
    ap.add_argument("--at", type=int, help="chat with the agent at this simulated hour instead of printing the table")
    ap.add_argument("-q", action="append", help="question to ask at --at (repeatable); omit to type your own")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config.STORM_REPLAY_MODEL = "v1" if args.pressure_only else "auto"
    rows = load(args.file)
    if args.pressure_only:
        rows = [{k: v for k, v in r.items() if k in ("ts", "pressure_hpa")} for r in rows]
    chat(rows, args.at, args.q) if args.at is not None else table(rows)


if __name__ == "__main__":
    main()
