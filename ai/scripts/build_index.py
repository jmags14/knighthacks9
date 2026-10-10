"""Build the guide search index (AGENT_PLAN §6.3).

    python ai/scripts/build_index.py            # Chroma at ai/data/chroma
    python ai/scripts/build_index.py --numpy    # lighter fallback (guides.npy + guides.json)

What it does:
  1. Read ai/knowledge/*.md (skip files starting with "_").
  2. Split each file into chunks: one "## " section = one chunk.
  3. Embed each chunk with nomic-embed-text (needs the Ollama server running).
  4. Save vectors + metadata so rag.py can search them.
Re-run whenever a guide changes.
"""
import json
import re
import sys
from pathlib import Path

# Let "python ai/scripts/build_index.py" find the ai package (repo root on sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml
from ai.agent import config

DOC_PREFIX = config.DOC_PREFIX       # nomic-embed-text wants these prefixes
QUERY_PREFIX = config.QUERY_PREFIX


# ---------------------------------------------------------------- parsing
def parse_guides(knowledge_dir=config.KNOWLEDGE_DIR):
    """Return a list of chunk dicts: id, title, heading, topic, source, url, text."""
    chunks = []
    for path in sorted(Path(knowledge_dir).glob("*.md")):
        if path.name.startswith("_"):                 # _README.md etc. are not guides
            continue
        # File = "---" front matter "---" body
        _, front, body = path.read_text(encoding="utf-8").split("---", 2)
        meta = yaml.safe_load(front)

        # Everything before the first "## " is ignored; each later piece is one chunk.
        sections = re.split(r"^## ", body, flags=re.M)[1:]
        for i, sec in enumerate(sections, 1):
            heading, *lines = sec.strip().splitlines()
            # "Source:" line -> metadata (keep the full text so answers can cite it)
            source = next((l[len("Source:"):].strip() for l in lines if l.startswith("Source:")), "")
            text = " ".join(l.strip() for l in lines if l.strip() and not l.startswith("Source:"))
            url = re.search(r"<(https?://[^>]+)>", source)
            chunks.append({
                "id": f"{path.stem}-{i:02d}",
                "title": meta["title"],
                "heading": heading.strip(),
                "topic": meta["topic"],
                "source": source,
                "url": url.group(1) if url else "",
                "text": text,
            })
    return chunks


def chunk_document(c):
    """The exact string we embed for one chunk."""
    return DOC_PREFIX + f"{c['title']}. {c['heading']}. {c['text']}"


# ---------------------------------------------------------------- embedding
def embed(texts):
    """Embed a list of strings (prefix already added). Returns a list of vectors."""
    import ollama
    return ollama.embed(model=config.EMBED_MODEL, input=texts)["embeddings"]


# ---------------------------------------------------------------- storing
def metadata_of(c):
    # Chroma metadata must be flat str/int values.
    return {k: c[k] for k in ("title", "heading", "topic", "source", "url")}


def store_chroma(chunks, vectors):
    import chromadb
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    try:                                              # start clean so re-runs don't duplicate
        client.delete_collection(config.CHROMA_COLLECTION)
    except Exception:
        pass
    col = client.get_or_create_collection(config.CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"})
    col.add(ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            embeddings=vectors,
            metadatas=[metadata_of(c) for c in chunks])
    return col.count()


def store_numpy(chunks, vectors):
    import numpy as np
    config.NUMPY_VECTORS.parent.mkdir(parents=True, exist_ok=True)
    arr = np.array(vectors, dtype="float32")
    arr /= np.linalg.norm(arr, axis=1, keepdims=True)  # normalized -> dot product = cosine
    np.save(config.NUMPY_VECTORS, arr)
    config.NUMPY_META.write_text(json.dumps(
        [{"id": c["id"], "text": c["text"], **metadata_of(c)} for c in chunks]), encoding="utf-8")
    return len(chunks)


# ---------------------------------------------------------------- test searches
TEST_SEARCHES = [   # (question, chunk id we hope comes back first)
    ("how much bleach per gallon", "water-04"),
    ("generator in garage", "generator-co-03"),
    ("is it over, it got calm", "hurricane-during-02"),
    ("is the chicken in the fridge safe", None),
    ("what is a basic emergency kit", None),
    ("someone has a deep cut", None),
]


def test_searches(chunks, vectors, k=3):
    """Tiny in-memory cosine search just to eyeball the results."""
    import numpy as np
    mat = np.array(vectors, dtype="float32")
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    for q, want in TEST_SEARCHES:
        qv = np.array(embed([QUERY_PREFIX + q])[0], dtype="float32")
        qv /= np.linalg.norm(qv)
        top = np.argsort(-(mat @ qv))[:k]
        hit = ""
        if want:
            hit = "  [OK]" if chunks[top[0]]["id"] == want else f"  [expected {want}]"
        print(f"\nQ: {q}{hit}")
        for j in top:
            print(f"   {mat[j] @ qv:.3f}  {chunks[j]['id']:<22} {chunks[j]['heading']}")


# ---------------------------------------------------------------- main
def main():
    chunks = parse_guides()
    print(f"Parsed {len(chunks)} chunks from {len({c['topic'] for c in chunks})} topics (expect 71)")
    vectors = embed([chunk_document(c) for c in chunks])
    print(f"Embedded with {config.EMBED_MODEL}: {len(vectors)} vectors of {len(vectors[0])} numbers")

    if "--numpy" in sys.argv:
        n = store_numpy(chunks, vectors)
        print(f"Saved {n} chunks to {config.NUMPY_VECTORS} + {config.NUMPY_META}")
    else:
        n = store_chroma(chunks, vectors)
        print(f"Chroma collection '{config.CHROMA_COLLECTION}' now holds {n} chunks at {config.CHROMA_DIR}")

    test_searches(chunks, vectors)


if __name__ == "__main__":
    main()
