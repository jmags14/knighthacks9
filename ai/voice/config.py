"""Voice settings in one place. Change a name here (no code changes) to switch models, e.g. on the
Pi use PIPER_VOICE = "en_US-lessac-low" (faster). Then re-run download_voice_models.py once."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# --- Speech to text (faster-whisper) ---
STT_MODEL = "tiny.en"            # tiny.en | base.en | small.en (bigger = slower, more accurate)
STT_COMPUTE = "int8"             # int8 = fastest on CPU
STT_BEAM_SIZE = 1                # 1 = greedy decoding, fastest
# A hint of what people will say. Tiny models drop or mangle rare words (e.g. "hectopascals"), so we
# prime Whisper with a few sample sentences using our vocabulary. Write it as normal sentences:
# a bare word list makes it output lowercase text with no punctuation. Measured on 24 clips:
# word recall 95% without the hint, 99% with it. Add sample sentences here, no code change needed.
STT_HINT = ("Is the storm over? How do I use a generator safely? Pressure is 996 hectopascals. "
            "Is there carbon monoxide? Do I need to evacuate?")

# --- Text to speech (Piper) ---
PIPER_VOICE = "en_US-lessac-medium"      # en_US-lessac-low is the faster voice for the Pi

# --- Recording ---
SAMPLE_RATE = 16000              # Whisper wants 16 kHz mono
MAX_RECORD_S = 10                # recording stops by itself after this long

# --- What the pipeline says when it can't understand ---
DIDNT_CATCH = "Sorry, I didn't catch that. Please try again."

# --- Paths (models are downloaded once into ai/models/voice, which git ignores) ---
VOICE_DIR = ROOT / "ai" / "models" / "voice"
STT_DIR = VOICE_DIR / f"whisper-{STT_MODEL}"
PIPER_ONNX = VOICE_DIR / f"{PIPER_VOICE}.onnx"          # the .onnx.json sits next to it


def piper_repo_path(voice=PIPER_VOICE):
    """'en_US-lessac-medium' -> 'en/en_US/lessac/medium/en_US-lessac-medium' (folder layout of rhasspy/piper-voices)."""
    lang, speaker, quality = voice.split("-")
    return f"{lang.split('_')[0]}/{lang}/{speaker}/{quality}/{voice}"
