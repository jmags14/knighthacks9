"""The voice pipeline: wav in -> words -> agent -> speech out.

Two entry points for CS 1's code on the Pi:
  handle_voice_turn(wav)  the ESP32 BUTTON handler calls this after recording a question.
  speak_event(event)      PROACTIVE alarms/phase/power events call this (no microphone involved).

Only one thing speaks at a time. An alarm interrupts an answer that is playing (the answer is cut
short, the alarm is spoken); two alarms simply take turns.
"""
import os
import threading
import time

from ai.agent import agent
from ai.voice import audio_io, config, stt, tts

_speaker = threading.Lock()     # whoever holds this is the one talking
_playing_kind = None            # "answer" or "alarm" while something is playing, else None


def _play(path, kind):
    """Wait for our turn to talk, then play. An alarm cuts off an answer instead of waiting for it."""
    global _playing_kind
    if kind == "alarm" and _playing_kind == "answer":
        audio_io.stop_playback()        # the answer's play_wav() returns at once and frees the lock
    with _speaker:                      # other speakers queue here
        _playing_kind = kind
        try:
            audio_io.play_wav(path)
        finally:
            _playing_kind = None


def _say(text, kind, play, timings):
    """text -> wav -> speakers. Records tts/play times. Returns the wav path if kept (play=False)."""
    t = time.perf_counter()
    path = tts.speak(text)
    timings["tts"] = _ms(t)
    if not play:
        return path                     # caller wants the file (push_to_talk --no-play)
    try:
        t = time.perf_counter()
        _play(path, kind)
        timings["play"] = _ms(t)
    finally:
        os.remove(path)                 # don't leave a wav behind after every sentence
    return None


def _ms(t0):
    return round((time.perf_counter() - t0) * 1000, 1)


def handle_voice_turn(wav_path, play=True):
    """Recorded question -> spoken answer. Returns what happened, for logging/printing:
    {"transcript","answer","route","trace_id","timings","wav"}"""
    timings = {}
    t = time.perf_counter()
    transcript = stt.transcribe(wav_path)
    timings["stt"] = _ms(t)

    # Nothing heard, or only noise/punctuation: ask the user to try again (no agent call).
    if not any(c.isalnum() for c in transcript):
        wav = _say(config.DIDNT_CATCH, "answer", play, timings)
        return {"transcript": transcript, "answer": config.DIDNT_CATCH, "route": "no_speech",
                "trace_id": None, "timings": timings, "wav": wav}

    t = time.perf_counter()
    r = agent.answer(transcript, "voice")
    timings["agent"] = _ms(t)
    wav = _say(r["answer"], "answer", play, timings)
    return {"transcript": transcript, "answer": r["answer"], "route": r["route"],
            "trace_id": r["trace_id"], "timings": timings, "wav": wav}


def speak_event(event, play=True):
    """Speak the explanation of an alarm / phase change / power event (templates, no LLM).
    A replay event keeps its 'Replay:' prefix because explain_event() already adds it.
    Returns the same shape as handle_voice_turn (answer == '' means there was nothing to say)."""
    timings = {}
    t = time.perf_counter()
    r = agent.explain_event(event)
    timings["agent"] = _ms(t)
    wav = _say(r["answer"], "alarm", play, timings) if r["answer"] else None
    return {"transcript": None, "answer": r["answer"], "route": r["route"],
            "trace_id": r["trace_id"], "timings": timings, "wav": wav}
