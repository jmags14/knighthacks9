"""P1: instant canned answers (AGENT_PLAN 5.2).

At first use we embed every canned question once and keep the vectors in memory.
Per question: embed it, find the closest canned question, accept if it is close
enough AND clearly better than the next *different* entry (sibling topics score
0.80-0.90 with nomic, so the margin rule avoids picking a related-but-wrong answer).
"""
import json

import numpy as np

from ai.agent import config, rag

_cache = None   # (matrix of normalized vectors, entry index per row, entries)


def load(path=config.CANNED_FILE):
    """Read canned.json and embed all questions. Called automatically on first match."""
    global _cache
    entries = json.loads(open(path, encoding="utf-8").read())
    rows = [(i, q) for i, e in enumerate(entries) for q in e["questions"]]
    mat = np.array(rag.embed_texts([q for _, q in rows]), dtype="float32")
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    _cache = (mat, [i for i, _ in rows], entries)
    return len(entries)


def best(text):
    """Closest canned entry, whether or not it is good enough:
    {"id","answer","source","score","runner_up"} (runner_up = best score of a different entry)."""
    if _cache is None:
        load()
    mat, owner, entries = _cache
    q = np.array(rag.embed_query(text), dtype="float32")
    sims = mat @ (q / np.linalg.norm(q))
    order = np.argsort(-sims)
    top = int(order[0])
    runner = next((float(sims[j]) for j in order if owner[j] != owner[top]), 0.0)
    e = entries[owner[top]]
    return {"id": e["id"], "answer": e["answer"], "source": e["source"],
            "score": float(sims[top]), "runner_up": runner}


def match(text, threshold=config.CANNED_THRESHOLD, margin=config.CANNED_MARGIN):
    """The canned entry for this question, or None."""
    b = best(text)
    if b["score"] >= threshold and b["score"] - b["runner_up"] >= margin:
        return b
    return None
