"""P1: instant canned answers (AGENT_PLAN 5.2).

The ~220 canned questions are embedded ONCE and saved to ai/data/canned.npy
(build_index.py does this, or load() does it the first time). After that a question
needs only ONE embedding call: embed it, compare to the saved vectors, done.

Match rule: the closest canned question must score high enough AND clearly beat the next
*different* entry (sibling topics score 0.80-0.90 with nomic, so a margin avoids picking a
related-but-wrong answer).
"""
import hashlib
import json
import time
from pathlib import Path

import numpy as np

from ai.agent import config, rag

BATCH = 32              # questions per embedding call while building the cache
RETRY_SECONDS = 30      # after a failed load, skip canned this long instead of timing out every question

_cache = None           # (matrix of normalized vectors, entry index per row, entries)
_retry_after = 0.0      # time.monotonic() value before which load() is not retried


def _fingerprint(raw):
    """Changes when canned.json or the embedding model changes, so a stale cache is rebuilt."""
    return hashlib.sha1(raw + config.EMBED_MODEL.encode()).hexdigest()


def build_cache(path=None):
    """Embed every canned question and save the vectors. Returns (matrix, owner, entries)."""
    raw = Path(path or config.CANNED_FILE).read_bytes()
    entries = json.loads(raw.decode("utf-8"))
    rows = [(i, q) for i, e in enumerate(entries) for q in e["questions"]]
    vectors = []
    for start in range(0, len(rows), BATCH):          # small batches: a cold model can be slow
        vectors += rag.embed_texts([q for _, q in rows[start:start + BATCH]],
                                   timeout=config.EMBED_LOAD_TIMEOUT_S)
    mat = np.array(vectors, dtype="float32")
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)  # normalized -> dot product = cosine
    owner = [i for i, _ in rows]
    config.CANNED_VECTORS.parent.mkdir(parents=True, exist_ok=True)
    np.save(config.CANNED_VECTORS, mat)
    config.CANNED_META.write_text(json.dumps({"hash": _fingerprint(raw), "owner": owner}), encoding="utf-8")
    return mat, owner, entries


def load(path=None):
    """Fill the in-memory cache: from disk if it matches canned.json, otherwise rebuild it."""
    global _cache, _retry_after
    raw = Path(path or config.CANNED_FILE).read_bytes()
    entries = json.loads(raw.decode("utf-8"))
    try:
        meta = (json.loads(config.CANNED_META.read_text(encoding="utf-8"))
                if config.CANNED_META.exists() and config.CANNED_VECTORS.exists() else None)
        if meta and meta.get("hash") == _fingerprint(raw):
            _cache = (np.load(config.CANNED_VECTORS), meta["owner"], entries)      # instant
        else:
            _cache = build_cache(path)
    except Exception:
        _retry_after = time.monotonic() + RETRY_SECONDS
        raise
    return len(entries)


def best(text):
    """Closest canned entry, whether or not it is good enough:
    {"id","answer","source","score","runner_up"} (runner_up = best score of a different entry)."""
    if _cache is None:
        if time.monotonic() < _retry_after:
            raise RuntimeError("canned cache unavailable, retrying soon")
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
