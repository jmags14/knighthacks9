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
EMBED_TIMEOUT_S = 10

# --- RAG / canned ---
RAG_K = 3                       # chunks given to the LLM
CANNED_THRESHOLD = 0.85         # cosine similarity needed for a canned answer (plan said 0.80; raised, see step 2)
CANNED_MARGIN = 0.03            # best canned entry must beat the next DIFFERENT entry by this much
RAG_UNSURE = 0.60               # best chunk below this = "I'm not sure, closest guide is X" (no LLM)
RAG_MIN_SIM = 0.50              # best guide chunk below this = off-topic question, no LLM call (tune in ask.py)
DOC_PREFIX = "search_document: "    # nomic-embed-text wants these prefixes
QUERY_PREFIX = "search_query: "
PROMPT_MAX_WORDS = 400

# --- Box location + barometer calibration (AGENT_PLAN §8.2) ---
ALTITUDE_M = 0.0                # meters above sea level where the box sits (~1 hPa per 8 m)
CAL_OFFSET_HPA = 0.0            # sea-level reading minus nearby airport/NWS reading, set once

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
BOX_DB = ROOT / "data" / "box.db"           # CS 1's SQLite file; traces go here if it exists
STORM_MODEL_FILE = ROOT / "ai" / "ml" / "storm_risk.json"
