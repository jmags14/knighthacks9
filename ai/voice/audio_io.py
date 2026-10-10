"""Laptop audio. Three tiny functions, deliberately isolated from everything else.

On the Pi, CS 1 replaces this whole file with arecord / aplay and the ESP32 button:
    record_until_enter()  -> returns the path of a 16 kHz mono 16-bit WAV
    play_wav(path)        -> blocks until the sound has finished
    stop_playback()       -> cuts the current sound short (so an alarm can interrupt an answer)
"""
import os
import sys
import tempfile
import time
import wave

import numpy as np
import sounddevice as sd

from ai.voice import config


def _enter_pressed():
    """True if the user hit Enter since we last looked (never blocks)."""
    if os.name == "nt":
        import msvcrt
        pressed = False
        while msvcrt.kbhit():                       # drain every waiting key
            pressed |= msvcrt.getwch() in ("\r", "\n")
        return pressed
    import select
    if select.select([sys.stdin], [], [], 0)[0]:    # Linux/macOS: stdin has a line waiting
        sys.stdin.readline()
        return True
    return False


def record_until_enter():
    """Record from the default microphone until Enter is pressed (or MAX_RECORD_S). Returns a WAV path."""
    frames = []

    def on_audio(indata, frame_count, time_info, status):   # called by sounddevice for each small block
        frames.append(indata.copy())

    with sd.InputStream(samplerate=config.SAMPLE_RATE, channels=1, dtype="int16", callback=on_audio):
        start = time.monotonic()
        while time.monotonic() - start < config.MAX_RECORD_S and not _enter_pressed():
            time.sleep(0.05)

    audio = np.concatenate(frames) if frames else np.zeros((0, 1), dtype="int16")
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="stormbox_rec_")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)                           # 16-bit
        w.setframerate(config.SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    return path


def play_wav(path):
    """Play a WAV file and wait until it finishes (or stop_playback() is called)."""
    with wave.open(str(path), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="int16").reshape(-1, channels)
    sd.play(data, rate)
    sd.wait()


def stop_playback():
    sd.stop()
