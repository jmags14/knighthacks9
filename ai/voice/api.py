"""Browser voice routes: the dashboard's mic button and the 🔊 speaker buttons call these.

    from ai.voice.api import router as voice_router
    app.include_router(voice_router)

  GET  /api/voice/status   are the Whisper and Piper models installed?
  POST /api/voice/stt      raw audio bytes (WAV from the browser)  -> {"text", "stt_ms", ...}
  POST /api/voice/tts      {"text", "trace_id"?}                   -> audio/wav bytes

Everything runs offline: faster-whisper (speech to text) and Piper (text to speech) are local models.
(The browser's own SpeechRecognition is NOT used: Chrome sends that audio to Google.)

Routes are `async def` only so they can read the upload; the slow model work runs in a thread
(asyncio.to_thread) so the server keeps answering other requests. The models import lazily, so the
server still starts on a machine without the voice packages (the routes then answer 503).
"""
import asyncio
import io
import os
import tempfile
import threading
import time
import wave

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ai.agent import trace
from ai.voice import config

router = APIRouter()

MAX_UPLOAD_BYTES = 5 * 1024 * 1024      # reject bigger uploads (a 15 s WAV is only ~0.5 MB)
MAX_AUDIO_SECONDS = 15                  # reject longer recordings
MAX_TTS_CHARS = 2000                    # reject absurdly long text

# One request at a time per model: Whisper and Piper are not built for parallel calls.
_stt_lock = threading.Lock()
_tts_lock = threading.Lock()

MISSING_MODELS = "Voice models are not installed. Run: python ai/scripts/download_voice_models.py"


def models_installed():
    return {"stt": config.STT_DIR.exists(), "tts": config.PIPER_ONNX.exists()}


# ---------------------------------------------------------------- the slow parts (run in a thread)
def _transcribe(path):
    from ai.voice import stt                    # lazy: importing faster-whisper takes a moment
    with _stt_lock:
        t0 = time.perf_counter()
        text = stt.transcribe(path)
        return text, round((time.perf_counter() - t0) * 1000, 1)


def _synthesize(text):
    from ai.voice import tts
    with _tts_lock:
        t0 = time.perf_counter()
        path = tts.speak(text)                  # cleans the text first (no "hPa", no source lines)
        try:
            with open(path, "rb") as f:
                return f.read(), round((time.perf_counter() - t0) * 1000, 1)
        finally:
            os.remove(path)


def warm_up():
    """Load Whisper and Piper now so the first mic press is not slow. main.py runs this in the background."""
    have = models_installed()
    try:
        if have["stt"]:
            from ai.voice import stt
            stt._load()
        if have["tts"]:
            from ai.voice import tts
            tts._load()
    except Exception:
        pass


# ---------------------------------------------------------------- helpers
async def _read_limited(request):
    """Read the upload but stop as soon as it is too big (don't trust Content-Length alone)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Recording is too large (max 5 MB).")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Recording is too large (max 5 MB).")
    return bytes(body)


def _wav_seconds(data):
    """Length of a WAV upload in seconds, or None if it is not a WAV."""
    if data[:4] != b"RIFF":
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except (wave.Error, EOFError):
        raise HTTPException(400, "That audio file is damaged. Please record again.")


# ---------------------------------------------------------------- routes
@router.get("/api/voice/status")
def status():
    """The dashboard asks this first, to hide the voice buttons if the models are missing."""
    have = models_installed()
    return {"stt_ready": have["stt"], "tts_ready": have["tts"],
            "stt_model": f"faster-whisper {config.STT_MODEL}", "tts_voice": f"Piper {config.PIPER_VOICE}",
            "max_seconds": MAX_AUDIO_SECONDS, "offline": True}


@router.post("/api/voice/stt")
async def speech_to_text(request: Request):
    if not models_installed()["stt"]:
        raise HTTPException(503, MISSING_MODELS)
    data = await _read_limited(request)
    if not data:
        raise HTTPException(400, "No audio received.")
    seconds = _wav_seconds(data)
    if seconds is not None and seconds > MAX_AUDIO_SECONDS:
        raise HTTPException(413, f"Recording is too long (max {MAX_AUDIO_SECONDS} seconds).")

    # faster-whisper reads from a file path. The suffix lets it pick the right decoder.
    suffix = ".wav" if seconds is not None else ".webm"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="stormbox_up_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        text, ms = await asyncio.to_thread(_transcribe, path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Could not transcribe that audio ({type(e).__name__}).")
    finally:
        os.remove(path)
    return {"text": text, "stt_ms": ms, "audio_seconds": seconds,
            "engine": f"faster-whisper {config.STT_MODEL}", "offline": True}


class TtsRequest(BaseModel):
    text: str
    trace_id: int | None = None         # if given, the speech time is added to that answer's trace


@router.post("/api/voice/tts")
async def text_to_speech(req: TtsRequest):
    if not models_installed()["tts"]:
        raise HTTPException(503, MISSING_MODELS)
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "text must not be empty")
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(413, f"Text is too long to speak (max {MAX_TTS_CHARS} characters).")
    try:
        wav, ms = await asyncio.to_thread(_synthesize, text)
    except Exception as e:
        raise HTTPException(500, f"Could not make speech ({type(e).__name__}).")
    if req.trace_id is not None:
        trace.add_timings(req.trace_id, {"tts": ms})
    return Response(content=wav, media_type="audio/wav", headers={"X-TTS-Ms": str(ms)})
