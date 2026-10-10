# StormBox knowledge pack

Offline guide text for the StormBox RAG. Every fact comes from an official or expert source, rewritten in plain words and credited. Files starting with `_` (like this one) are **not** indexed.

## Files

| File | Topic tag | Router uses it for |
|---|---|---|
| `flood.md` | `flood` | flood alarm, water on the floor, flood water, cleanup |
| `hurricane-during.md` | `storm` | storm alarm, where to shelter, the eye, tornadoes |
| `generator-co.md` | `air` | air alarm, generators, carbon monoxide |
| `heat.md` | `heat` | heat warning, heat stroke, staying cool |
| `power-outage.md` | `power` | outage basics, surges, medical devices |
| `food-safety.md` | `food` | fridge/freezer, what to throw away |
| `water.md` | `water` | how much water, boiling, bleach |
| `evacuation.md` | `evacuation` | leave or stay, routes, returning |
| `after-the-storm.md` | `after` | power lines, damaged homes, tap water, pests |
| `first-aid.md` | `first-aid` | bleeding, wounds |
| `pets.md` | `pets` | pets during storms |
| `medications.md` | `medical` | insulin, medicines, powered medical devices |
| `communication.md` | `communication` | 911, texting, phone battery, stress helpline |
| `emergency-kit.md` | `kit` | supply kit |
| `stormbox-readings.md` | `stormbox` | what the box's pressure, phases, risk score, alarms mean |

Alarm id → topic filter: `flood`→`flood`, `air`→`air`, `heat`→`heat`, `storm`→`storm`.

## Format (what `scripts/build_index.py` should expect)

```markdown
---
title: Floods
topic: flood
sources:
  - name: Ready.gov (FEMA) - Floods
    url: https://www.ready.gov/floods
retrieved: 2026-10-08
---

## Section heading       <- each "##" section = one chunk (about 60-170 words)
Body text in plain words...
Source: Ready.gov (FEMA) - Floods <https://www.ready.gov/floods>   <- last line: this chunk's source(s)
```

- Front matter is simple YAML (`pip install pyyaml`, or a few lines of string parsing).
- Each chunk's **last line** starts with `Source:`. Strip it from the text you embed and store it as the chunk's `source` metadata, so answers cite the exact page.
- Chunk id: `<file name>-<section number>`, e.g. `flood-02`.
- Embed `"search_document: " + title + ". " + heading + ". " + body` with `nomic-embed-text`, and the question as `"search_query: " + question`.

Minimal parsing sketch:

```python
import re, pathlib, yaml
for path in sorted(pathlib.Path("knowledge").glob("*.md")):
    if path.name.startswith("_"):
        continue
    _, front, body = path.read_text().split("---", 2)
    meta = yaml.safe_load(front)
    for i, sec in enumerate(re.split(r"^## ", body, flags=re.M)[1:], 1):
        heading, *lines = sec.strip().splitlines()
        src = next((l[7:].strip() for l in lines if l.startswith("Source:")), "")
        text = " ".join(l for l in lines if l.strip() and not l.startswith("Source:"))
        chunk = {"id": f"{path.stem}-{i:02d}", "title": meta["title"], "heading": heading,
                 "topic": meta["topic"], "source": src, "text": text}
```

## Rules for adding or editing content

1. Use official or expert sources: Ready.gov/FEMA, CDC, NWS/NOAA, EPA, FDA, USDA/FoodSafety.gov, FCC, Florida Division of Emergency Management, American Red Cross.
2. **Rewrite in your own words.** Don't paste paragraphs. Keep the numbers exact.
3. One idea per `##` section, written so it makes sense on its own (the agent only sees 3 chunks at a time).
4. End every section with a `Source:` line.
5. Re-run `scripts/build_index.py` after any change.

## Honesty notes

- StormBox's air sensor is **not** a carbon monoxide detector. The text says so wherever CO comes up.
- StormBox gives general safety information, not medical advice, and cannot call 911.
- Guidance can change. Sources were checked on 2026-10-08; re-check before a real deployment.
- `stormbox-readings.md` describes the box itself (team-written). The pressure ranges there are typical values used by the project, not an official NWS table.
