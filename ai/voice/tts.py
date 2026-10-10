"""Text to speech with Piper (offline). speak(text) -> path of a WAV file.

clean_text() is separate and pure so it is easy to test: written answers contain things
that sound wrong read aloud (source lines, "°F", "hPa").
"""
import os
import re
import tempfile
import wave

from ai.voice import config

_voice = None


def clean_text(text):
    """Make written text sound right when spoken. 'Replay:' prefixes are kept on purpose."""
    # 1. drop source lines ("Source: flood.md", "Sources: ...", "(Source: ...)")
    lines = [ln for ln in text.splitlines() if not re.match(r"\s*\(?\s*sources?\b\s*[:\-]", ln, re.I)]
    text = " ".join(lines)
    # 2. units. "°F" / "29 C" / "84 F" -> degrees; do it before the plain-letter rule below
    text = re.sub(r"(\d)\s*°\s*F\b", r"\1 degrees Fahrenheit", text)
    text = re.sub(r"(\d)\s*°\s*C\b", r"\1 degrees Celsius", text)
    text = re.sub(r"(\d)\s+F\b", r"\1 degrees Fahrenheit", text)      # templates write "84 F"
    text = re.sub(r"(\d)\s+C\b", r"\1 degrees Celsius", text)
    text = text.replace("°", " degrees ")                              # any leftover degree sign
    text = re.sub(r"\bhPa\b", "hectopascals", text)
    text = re.sub(r"\bmph\b", "miles per hour", text)
    # 3. markdown characters that would be read out loud or confuse the voice
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _load():
    """Load the Piper voice from the local folder, once."""
    global _voice
    if _voice is None:
        from piper import PiperVoice            # imported here so tests of clean_text need no Piper
        _voice = PiperVoice.load(config.PIPER_ONNX)     # also reads the .onnx.json next to it
    return _voice


def speak(text):
    """Clean the text, synthesize it, and return the WAV path (the caller plays and deletes it)."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="stormbox_tts_")
    os.close(fd)
    with wave.open(path, "wb") as w:
        _load().synthesize_wav(clean_text(text), w)
    return path
