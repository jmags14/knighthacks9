"""The browser side of voice: the WAV encoder in voice.js (run with Node) and offline-safety checks on the page."""
import json
import re
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

from ai.agent import config

STATIC = config.ROOT / "pi" / "web" / "static"
JS_CHECK = Path(__file__).parent / "js" / "encode_wav_check.js"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")


@needs_node
def test_voice_js_has_no_syntax_errors():
    subprocess.run(["node", "--check", str(STATIC / "js" / "voice.js")], check=True)


@needs_node
def test_encode_wav_makes_a_valid_16khz_mono_wav(tmp_path):
    out = tmp_path / "enc.wav"
    run = subprocess.run(["node", str(JS_CHECK), str(out)], capture_output=True, text=True, check=True)
    info = json.loads(run.stdout)
    assert info["bytes"] == 44 + 7 * 2
    assert info["maxSeconds"] == 14 and info["maxSeconds"] < 15           # stops before the server's 15 s limit
    with wave.open(str(out), "rb") as w:                                  # Python's own parser accepts it
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 7)
        raw = w.readframes(7)
    values = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 2)]
    assert values[0] == 0
    assert values[1] == 32767 and values[2] == -32768                     # full scale both ways
    assert values[3] == 32767 and values[4] == -32768                     # louder than full scale is clipped, not wrapped
    assert values[5] == pytest.approx(16383, abs=1) and values[6] == pytest.approx(-8192, abs=1)


@needs_node
def test_encoded_wav_is_accepted_by_the_server_length_check(tmp_path):
    from ai.voice.api import _wav_seconds
    out = tmp_path / "enc.wav"
    subprocess.run(["node", str(JS_CHECK), str(out)], check=True, capture_output=True)
    assert _wav_seconds(out.read_bytes()) == pytest.approx(7 / 16000)


def test_ask_page_has_the_mic_and_speaker_pieces():
    html = (STATIC / "ask.html").read_text(encoding="utf-8")
    for needle in ('id="mic-btn"', 'id="voice-status"', 'id="auto-speak"', 'src="js/voice.js"',
                   "/api/voice/tts", "Heard by Whisper (offline)", "stt_ms", 'channel: "voice"'):
        assert needle in html, needle


def test_voice_js_posts_to_the_offline_stt_route():
    js = (STATIC / "js" / "voice.js").read_text(encoding="utf-8")
    assert "/api/voice/stt" in js and "audio/wav" in js


def test_the_page_never_uses_browser_speech_recognition():
    """Chrome's SpeechRecognition sends audio to Google. The box must stay offline."""
    for name in ("ask.html", "js/voice.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"new\s+(webkit)?SpeechRecognition", text), name
        assert "speechSynthesis" not in text, name                        # the browser's own TTS voices are not our Piper


def test_the_page_loads_nothing_from_the_internet():
    for name in ("ask.html", "js/voice.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        assert not re.search(r"""(src|href)\s*=\s*["']https?://""", text), name   # no CDN scripts, fonts or styles
        assert not re.search(r"""fetch\(\s*["']https?://""", text), name          # no calls to other servers
