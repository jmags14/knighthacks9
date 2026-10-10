"""Guide parser (build_index.parse_guides) and canned.json checks."""
import json
import re

from ai.agent import config
from ai.scripts.build_index import chunk_document, parse_guides

TOPICS = {"flood", "storm", "air", "heat", "power", "food", "water", "evacuation", "after",
          "first-aid", "pets", "medical", "communication", "kit", "stormbox"}


def test_71_chunks_from_15_topics():
    chunks = parse_guides()
    assert len(chunks) == 71
    assert {c["topic"] for c in chunks} == TOPICS


def test_every_chunk_has_a_source():
    for c in parse_guides():
        assert c["source"], f"{c['id']} has no Source line"


def test_chunk_fields_are_clean():
    chunks = parse_guides()
    assert len({c["id"] for c in chunks}) == 71                      # unique ids
    for c in chunks:
        assert re.fullmatch(r"[a-z-]+-\d\d", c["id"])               # e.g. flood-02
        assert c["heading"] and len(c["text"].split()) > 20
        assert "Source:" not in c["text"]                            # source line is stripped from the text
        assert "##" not in c["text"]


def test_underscore_files_are_skipped():
    assert not any(c["id"].startswith("_") for c in parse_guides())


def test_known_ids_from_the_plan():
    by_id = {c["id"]: c for c in parse_guides()}
    assert "bleach" in by_id["water-04"]["text"].lower()
    assert "generator" in by_id["generator-co-03"]["heading"].lower()
    assert "eye" in by_id["hurricane-during-02"]["heading"].lower()
    assert by_id["water-04"]["url"].startswith("https://")


def test_document_text_has_prefix_title_heading():
    c = parse_guides()[0]
    doc = chunk_document(c)
    assert doc.startswith("search_document: ")
    assert c["title"] in doc and c["heading"] in doc


def test_canned_json_is_valid_and_sourced():
    entries = json.loads(config.CANNED_FILE.read_text(encoding="utf-8"))
    assert 40 <= len(entries) <= 60
    assert len({e["id"] for e in entries}) == len(entries)
    all_sources = " ; ".join(c["source"] for c in parse_guides())
    for e in entries:
        assert 3 <= len(e["questions"]) <= 5, e["id"]
        assert e["answer"].strip(), e["id"]
        for name in e["source"].split(";"):                         # every cited source exists in the guides
            assert name.strip() in all_sources, (e["id"], name)
