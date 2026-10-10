"""LLM benchmark (AGENT_PLAN 11): the same 20 questions through both models.

    python ai/scripts/benchmark_llm.py                 # both models, all 20 questions
    python ai/scripts/benchmark_llm.py --models llama3.2:1b --limit 5
    python ai/scripts/benchmark_llm.py --gated         # skip questions the agent would not send to the LLM

Each question goes through the REAL P3 pipeline pieces (topic guess -> guide search -> prompt),
but skips the router and canned answers so the LLM is what gets measured.
It records seconds to the full answer, tokens/second, and "grounding": the share of the
answer's longer words that appear in the passages or question (a rough "did it stay on the
passages" check; read the answers too). Run it on the real Pi to decide, then keep the laptop
numbers only as a smoke test.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ollama
from ai.agent import config, prompt, rag, router

QUESTIONS = [
    "Is the chicken in the fridge safe after 6 hours without power?",
    "Can I run a generator in the garage with the door open?",
    "Water is coming under the door, what should I do?",
    "How do I make water safe to drink?",
    "My phone is at 10%, how do I save battery?",
    "Is it safe to walk through flood water?",
    "Someone has a deep cut, what do I do?",
    "How do I keep insulin cool without power?",
    "What should I do with my dog during the storm?",
    "When should we evacuate?",
    "A power line is down in the street, what now?",
    "It's 95 degrees inside, how do we stay cool?",
    "Should I open windows to equalize pressure?",
    "How do I know if the storm is really over?",
    "The air alarm went off but I don't smell anything.",
    "Can I use a gas stove to heat the house?",
    "What's in a basic emergency kit?",
    "How do I turn off the electricity at the breaker safely?",
    "What do I do after the storm passes?",
    "What's the capital of France?",   # should say it can only help with emergency topics
]

# Calm fake box so every model sees the same READINGS line.
STATE = {"pressure_hpa": 1013.0, "temp_c": 30.0, "wall_power": 0, "alarms": [], "storm": {"phase": "steady"}}

STOP = set("""about above after again also because before being could does from have into more most
other should still than that their them then there these they this those through under until
using when where which while will with without would your""".split())


def grounding(answer, context):
    """Share of the answer's words (5+ letters, not common words) that appear in the context."""
    words = [w for w in re.findall(r"[a-z]{5,}", answer.lower()) if w not in STOP]
    if not words:
        return None
    ctx = context.lower()
    return round(sum(w in ctx for w in words) / len(words), 2)


def generate(model, text, timeout):
    """Same call settings as agent._generate, but keeps the speed stats."""
    client = ollama.Client(timeout=timeout)
    extra = {"think": False} if model.startswith("qwen3") else {}
    t0 = time.perf_counter()
    r = client.generate(model=model, prompt=text, keep_alive=config.LLM_KEEP_ALIVE,
                        options={"num_predict": config.LLM_NUM_PREDICT,
                                 "temperature": config.LLM_TEMPERATURE,
                                 "num_ctx": config.LLM_NUM_CTX}, **extra)
    secs = time.perf_counter() - t0
    out = re.sub(r"<think>.*?</think>", "", r["response"], flags=re.S).strip()
    toks = r.get("eval_count") or 0
    eval_s = (r.get("eval_duration") or 0) / 1e9
    return out, secs, (toks / eval_s if eval_s else None), toks


def run_model(model, items, timeout):
    print(f"\n=== {model} ===")
    t0 = time.perf_counter()
    try:
        generate(model, "Say OK.", timeout)                 # load the model; not counted below
    except Exception as e:
        print(f"  cannot use {model}: {type(e).__name__}: {e}")
        return []
    print(f"  load + warm-up: {time.perf_counter() - t0:.1f}s")
    rows = []
    for i, it in enumerate(items, 1):
        try:
            ans, secs, tps, toks = generate(model, it["prompt"], timeout)
            err = None
        except Exception as e:
            ans, secs, tps, toks, err = "", timeout, None, 0, type(e).__name__
        g = grounding(ans, it["context"])
        rows.append({"n": it["n"], "question": it["q"], "seconds": round(secs, 2), "tok_per_s": tps and round(tps, 1),
                     "tokens": toks, "grounding": g, "gate": it["gate"], "error": err, "answer": ans})
        print(f"  {it['n']:>2}. {secs:5.1f}s  {tps or 0:5.1f} tok/s  ground={g}  gate={it['gate']:<7} "
              f"{(err or ans)[:90]!r}")
    try:                                                    # unload so the next model has the memory
        ollama.Client().generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass
    return rows


def summarize(name, rows):
    ok = [r for r in rows if not r["error"]]
    if not ok:
        return f"{name}: no successful answers"
    secs = sorted(r["seconds"] for r in ok)
    gr = [r["grounding"] for r in ok if r["grounding"] is not None]
    return (f"{name}: median {secs[len(secs) // 2]:.1f}s, slowest {secs[-1]:.1f}s, "
            f"avg grounding {sum(gr) / len(gr):.2f}, errors {len(rows) - len(ok)}/{len(rows)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=[config.LLM_MODEL, config.LLM_FALLBACK])
    ap.add_argument("--limit", type=int, help="only the first N questions")
    ap.add_argument("--timeout", type=float, default=120, help="seconds per call (benchmark is patient)")
    ap.add_argument("--gated", action="store_true", help="skip questions agent.py would answer without the LLM")
    ap.add_argument("--out", default=str(config.ROOT / "ai" / "data" / "benchmark_results.json"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # Build every prompt once so both models see exactly the same input.
    items = []
    for n, q in enumerate(QUESTIONS[:args.limit], 1):
        topic = router.guess_topic(q)
        chunks = rag.search(q, topic, config.RAG_K)
        top = chunks[0]["score"] if chunks else 0
        gate = "no_match" if top < config.RAG_MIN_SCORE else "llm"
        if args.gated and gate != "llm":
            print(f"  {n:>2}. skipped ({gate}, top score {top:.2f}): {q}")
            continue
        items.append({"n": n, "q": q, "gate": gate, "prompt": prompt.build_prompt(q, STATE, chunks, topic),
                      "context": q + " " + " ".join(c["text"] for c in chunks)})
    print(f"{len(items)} questions, models: {', '.join(args.models)}")

    results = {m: run_model(m, items, args.timeout) for m in args.models}
    print("\n--- summary ---")
    for m, rows in results.items():
        print(summarize(m, rows))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"Full answers saved to {args.out} (read them: grounding is only a rough check)")


if __name__ == "__main__":
    main()
