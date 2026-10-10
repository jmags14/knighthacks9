"""StormBox agent settings. One place for model names, timeouts and paths."""
from pathlib import Path

# Repo root = two folders above this file (ai/agent/config.py -> repo root).
# All paths hang off it, so scripts work no matter where you run them from.
ROOT = Path(__file__).resolve().parents[2]

# --- Models (AGENT_PLAN §9) ---
LLM_MODEL = "llama3.2:1b"       # primary answer-wording model
LLM_FALLBACK = "qwen3:0.6b"     # used if primary is missing or too slow (call with think=False)
EMBED_MODEL = "nomic-embed-text"

# --- Timeouts / LLM options ---
LLM_TIMEOUT_S = 15              # P3 answer slower than this -> try fallback
LLM_NUM_PREDICT = 120           # max tokens in an answer
LLM_TEMPERATURE = 0.2
LLM_KEEP_ALIVE = "24h"          # keep the model loaded between questions
LLM_NUM_CTX = 1024              # context window: prompt (<=~600 tokens) + answer fits; smaller = faster load/prefill
EMBED_TIMEOUT_S = 10            # one question embedding (model already loaded)
EMBED_LOAD_TIMEOUT_S = 60       # first embedding / cache build (model may need to load)

# --- RAG / canned ---
RAG_K = 3                       # chunks given to the LLM
CANNED_THRESHOLD = 0.85         # cosine similarity needed for a canned answer (plan said 0.80; raised, see step 2)
CANNED_MARGIN = 0.03            # best canned entry must beat the next DIFFERENT entry by this much
RAG_MIN_SCORE = 0.70            # best guide chunk below this = no_match: no LLM call (tune in ask.py)
DOC_PREFIX = "search_document: "    # nomic-embed-text wants these prefixes
QUERY_PREFIX = "search_query: "
PROMPT_MAX_WORDS = 400

# --- Box location + barometer calibration (AGENT_PLAN §8.2) ---
ALTITUDE_M = 0.0                # meters above sea level where the box sits (~1 hPa per 8 m)
CAL_OFFSET_HPA = 0.0            # sea-level reading minus nearby airport/NWS reading, set once

# --- Storm risk model choice (ai/ml/storm_job.py). Defaults = exactly the original v1 pressure model ---
STORM_MODEL_MODE = "v1"         # LIVE data. "v1" = pressure model only | "auto" = pick v1/v2 by which sensors work
STORM_REPLAY_MODEL = "auto"     # REPLAY demo. "v1" = pressure only | "auto" = v2 "both" if the replay has wind columns
WIND_RAW_UNIT = "ms"            # unit of the box's bare "wind"/"gust" field: "ms" (m/s, what the sensor reports) or "mph".
                                # The agent speaks mph, so it converts. Fields named wind_ms / wind_mph are never ambiguous.
SENSOR_ONLINE_MIN = 30          # a sensor counts as working if it sent a good reading in the last N minutes
GUST_WINDOW_S = 5               # gust = highest N-second average (NOAA style) when readings come every second
STORM_V2_DIR = ROOT / "ai" / "ml" / "v2"
STORM_V2_FILES = {"pressure": "storm_risk_pressure.json", "wind": "storm_risk_wind.json",
                  "both": "storm_risk_both.json"}

# --- Water calculator ---
GAL_PER_PERSON_DAY = 1.0        # Ready.gov
PET_GAL = 0.25                  # gallons per pet per day (check pets.md)

# --- Paths (relative to repo root) ---
KNOWLEDGE_DIR = ROOT / "ai" / "knowledge"
CANNED_FILE = KNOWLEDGE_DIR / "canned.json"
CHROMA_DIR = ROOT / "ai" / "data" / "chroma"
CHROMA_COLLECTION = "guides"
NUMPY_VECTORS = ROOT / "ai" / "data" / "guides.npy"     # fallback index
NUMPY_META = ROOT / "ai" / "data" / "guides.json"
CANNED_VECTORS = ROOT / "ai" / "data" / "canned.npy"    # precomputed canned-question embeddings
CANNED_META = ROOT / "ai" / "data" / "canned_meta.json"  # fingerprint + owner of each vector row
BOX_DB = ROOT / "data" / "box.db"           # CS 1's SQLite file; traces go here if it exists
STORM_MODEL_FILE = ROOT / "ai" / "ml" / "storm_risk.json"
