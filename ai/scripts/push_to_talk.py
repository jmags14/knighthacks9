"""Push-to-talk on the laptop: the same flow the Pi's button will trigger.

    python ai/scripts/push_to_talk.py                 # live fake box
    python ai/scripts/push_to_talk.py --state storm   # calm|storm|flood|eye|outage|replay
    python ai/scripts/push_to_talk.py --no-play       # don't use the speakers (wavs are kept, paths printed)

Press Enter to start recording, Enter again to stop (max 10 s). Then it prints what it heard,
the route, and how long each step took.
Type  /event flood  (or heat, storm, severe, wind, windalarm, eye, recovering, power) to hear an
alarm explanation instead.   /state storm  switches the fake box.   /quit  exits.
"""
import argparse
import copy
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ask import EVENTS, make_state              # the same fake boxes and events as ask.py

from ai.agent import agent, tools
from ai.voice import audio_io, pipeline, stt, tts

current = {"state": make_state("calm")}


def show(r):
    if r["transcript"] is not None:
        print(f"  heard    : {r['transcript']!r}")
    print(f"  answer   : {r['answer']}")
    print(f"  route    : {r['route']}   (trace {r['trace_id']})")
    steps = [("record", r["timings"].get("record")), ("stt", r["timings"].get("stt")),
             ("agent", r["timings"].get("agent")), ("tts", r["timings"].get("tts")),
             ("play", r["timings"].get("play"))]
    print("  timings  : " + "  ".join(f"{n} {ms:.0f} ms" for n, ms in steps if ms is not None))
    if r["wav"]:
        print(f"  wav      : {r['wav']}")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="calm", help="calm|storm|flood|eye|outage|replay")
    ap.add_argument("--no-play", action="store_true", help="don't play audio; keep the wav files")
    args = ap.parse_args()
    play = not args.no_play
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    current["state"] = make_state(args.state)
    tools.set_provider(lambda: copy.deepcopy(current["state"]))
    print("Loading models (first time takes a few seconds) ...")
    agent.warm_up()                              # canned vectors + embedding model + LLM
    stt._load()                                  # load Whisper now, not on your first question
    tts._load()                                  # load the Piper voice now too
    print(f"Ready (fake state: {args.state}).  Enter = record,  /event NAME,  /state NAME,  /quit")

    while True:
        try:
            line = input("\n> ").strip()
        except EOFError:
            break
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/state"):
            name = line.partition(" ")[2].strip() or "calm"
            try:
                current["state"] = make_state(name)
                print(f"  state is now '{name}'")
            except ValueError as e:
                print(f"  {e}")
        elif line.startswith("/event"):
            name = line.partition(" ")[2].strip()
            if name in EVENTS:
                show(pipeline.speak_event(EVENTS[name], play))
            else:
                print("  events: " + ", ".join(EVENTS))
        elif line == "":                         # plain Enter = start recording
            print("  recording ... press Enter to stop")
            t = time.perf_counter()
            wav = audio_io.record_until_enter()
            record_ms = round((time.perf_counter() - t) * 1000, 1)
            r = pipeline.handle_voice_turn(wav, play)
            r["timings"] = {"record": record_ms, **r["timings"]}
            show(r)
            Path(wav).unlink(missing_ok=True)
        else:
            print("  Press Enter to record, or type /event NAME, /state NAME, /quit")


if __name__ == "__main__":
    main()
