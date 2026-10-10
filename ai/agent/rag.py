"""Guide search (RAG retrieval). Embeds the question, finds the closest guide chunks.

Backend: Chroma (ai/data/chroma) if it exists, else the numpy files from
`build_index.py --numpy`. Both give the same results at this size.
"""
import json
import re
from functools import lru_cache

import numpy as np
import ollama

from ai.agent import config

_chroma_col = None      # cached Chroma collection
_np_index = None        # cached (vectors, metadata list)


# ---------------------------------------------------------------- embeddings
def embed_texts(texts, prefix=config.QUERY_PREFIX):
    """Embed strings with nomic-embed-text. Raises if the Ollama server is not reachable."""
    client = ollama.Client(timeout=config.EMBED_TIMEOUT_S)
    return client.embed(model=config.EMBED_MODEL, input=[prefix + t for t in texts])["embeddings"]


@lru_cache(maxsize=64)
def embed_query(text):
    """One question -> vector (tuple). Cached so canned + RAG share one embedding call."""
    return tuple(embed_texts([text], config.QUERY_PREFIX)[0])


# ---------------------------------------------------------------- helpers
def clean_source(source):
    """'Ready.gov <https://..>; EPA <https://..>' -> 'Ready.gov; EPA' (for speech/display)."""
    return re.sub(r"\s*<https?://[^>]*>", "", source or "").strip()


def sources_of(chunks):
    """Unique [{"title","source","url"}] for the chunks used in an answer."""
    seen, out = set(), []
    for c in chunks:
        key = (c["title"], c["source"])
        if key not in seen:
            seen.add(key)
            out.append({"title": c["title"], "source": clean_source(c["source"]), "url": c.get("url", "")})
    return out


# ---------------------------------------------------------------- backends
def _search_chroma(qv, topic, k):
    global _chroma_col
    if _chroma_col is None:
        import chromadb
        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        _chroma_col = client.get_collection(config.CHROMA_COLLECTION)
    res = _chroma_col.query(query_embeddings=[list(qv)], n_results=k,
                            where={"topic": topic} if topic else None)
    hits = []
    for i, cid in enumerate(res["ids"][0]):
        meta = res["metadatas"][0][i]
        hits.append({"id": cid, "text": res["documents"][0][i],
                     "score": 1 - res["distances"][0][i], **meta})   # cosine distance -> similarity
    return hits


def _search_numpy(qv, topic, k):
    global _np_index
    if _np_index is None:
        _np_index = (np.load(config.NUMPY_VECTORS),
                     json.loads(config.NUMPY_META.read_text(encoding="utf-8")))
    mat, meta = _np_index
    q = np.array(qv, dtype="float32")
    sims = mat @ (q / np.linalg.norm(q))               # stored vectors are already normalized
    order = [i for i in np.argsort(-sims) if not topic or meta[i]["topic"] == topic][:k]
    return [{**meta[i], "score": float(sims[i])} for i in order]


def _search_vec(qv, topic, k):
    if config.CHROMA_DIR.exists():
        try:
            return _search_chroma(qv, topic, k)
        except Exception:
            if not config.NUMPY_VECTORS.exists():
                raise
    if config.NUMPY_VECTORS.exists():
        return _search_numpy(qv, topic, k)
    raise RuntimeError("Guide index not built. Run: python ai/scripts/build_index.py")


# ---------------------------------------------------------------- public
def search(question, topic=None, k=config.RAG_K):
    """Top-k chunks for a question, best first. Each: id, title, heading, topic, source, url, text, score.
    If a topic filter returns fewer than 2 chunks, search again without it (AGENT_PLAN 5.4)."""
    qv = embed_query(question)
    hits = _search_vec(qv, topic, k)
    if topic and len(hits) < 2:
        hits = _search_vec(qv, None, k)
    return hits
