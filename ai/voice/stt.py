"""Speech to text. The rest of the code only calls transcribe(wav_path) -> str,
so swapping faster-whisper for Vosk later means rewriting just this file."""
import os

# Fully offline: the library must never try to reach Hugging Face. Set BEFORE importing it.
os.environ["HF_HUB_OFFLINE"] = "1"

from faster_whisper import WhisperModel

from ai.voice import config

_model = None


def _load():
    """Load the model from the local folder, once. Takes a second or two the first time."""
    global _model
    if _model is None:
        _model = WhisperModel(str(config.STT_DIR), device="cpu", compute_type=config.STT_COMPUTE,
                              local_files_only=True)
    return _model


def transcribe(wav_path):
    """WAV file -> text ('' if nobody spoke)."""
    segments, _info = _load().transcribe(
        str(wav_path), language="en", beam_size=config.STT_BEAM_SIZE,
        vad_filter=True, initial_prompt=config.STT_HINT)                # vad_filter skips silence, so quiet clips give no hallucinated text
    return " ".join(s.text.strip() for s in segments).strip()
