"""Voice tests: text cleaning, and the pipeline with STT / TTS / speakers mocked (no models, no audio)."""
import threading

import pytest

from ai.agent import agent
from ai.voice import config, pipeline, tts


# ---------------------------------------------------------------- text cleaning
@pytest.mark.parametrize("raw, expected", [
    ("It is 84°F outside.", "It is 84 degrees Fahrenheit outside."),
    ("It is 29°C.", "It is 29 degrees Celsius."),
    ("temperature 29 C, 84 F", "temperature 29 degrees Celsius, 84 degrees Fahrenheit"),   # template style
    ("Pressure is 996.0 hPa.", "Pressure is 996.0 hectopascals."),
    ("wind 45 mph", "wind 45 miles per hour"),
    ("gust is 26.2 m/s (about 59 mph)", "gust is 26.2 meters per second (about 59 miles per hour)"),
    ("**Stay** inside.", "Stay inside."),
])
def test_clean_text_units_and_markdown(raw, expected):
    assert tts.clean_text(raw) == expected


def test_clean_text_drops_source_lines():
    raw = "Boil water for one minute.\nSource: water.md\n(Sources: flood.md; heat.md)\nThen let it cool."
    assert tts.clean_text(raw) == "Boil water for one minute. Then let it cool."


def test_clean_text_keeps_replay_prefix_and_normal_words():
    assert tts.clean_text("Replay: Flood alarm. Move to higher ground.") == \
        "Replay: Flood alarm. Move to higher ground."
    assert "Fahrenheit" not in tts.clean_text("Take the C batteries")      # a lone "C" is not Celsius


# ---------------------------------------------------------------- pipeline with fakes
@pytest.fixture
def fakes(monkeypatch):
    """Replace STT, TTS and the speakers. `log` records what happened, in order."""
    log = {"spoken": [], "played": [], "asked": [], "transcript": "is the storm over"}
    monkeypatch.setattr(pipeline.stt, "transcribe", lambda wav: log["transcript"])
    monkeypatch.setattr(pipeline.tts, "speak", lambda text: (log["spoken"].append(text), f"/tmp/{len(log['spoken'])}.wav")[1])
    monkeypatch.setattr(pipeline.audio_io, "play_wav", lambda p: log["played"].append(p))
    monkeypatch.setattr(pipeline.os, "remove", lambda p: None)            # the fake wavs do not exist

    def fake_answer(text, channel="chat"):
        log["asked"].append((text, channel))
        return {"answer": "Not yet.", "sources": [], "route": "tool", "trace_id": 7}
    monkeypatch.setattr(agent, "answer", fake_answer)
    return log


def test_voice_turn_speaks_the_agent_answer(fakes):
    r = pipeline.handle_voice_turn("q.wav")
    assert fakes["asked"] == [("is the storm over", "voice")]             # channel is "voice"
    assert fakes["spoken"] == ["Not yet."] and len(fakes["played"]) == 1
    assert r["route"] == "tool" and r["trace_id"] == 7
    assert set(r["timings"]) == {"stt", "agent", "tts", "play"}


@pytest.mark.parametrize("heard", ["", "   ", "...", "?!"])
def test_unclear_transcript_says_try_again_without_asking_the_agent(fakes, heard):
    fakes["transcript"] = heard
    r = pipeline.handle_voice_turn("q.wav")
    assert fakes["asked"] == []
    assert fakes["spoken"] == [config.DIDNT_CATCH]
    assert r["route"] == "no_speech"


def test_no_play_keeps_the_wav_and_does_not_play(fakes):
    r = pipeline.handle_voice_turn("q.wav", play=False)
    assert fakes["played"] == [] and r["wav"] == "/tmp/1.wav"


def test_speak_event_says_the_alarm_text(fakes):
    r = pipeline.speak_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})
    assert r["route"] == "proactive" and r["answer"] and fakes["spoken"] == [r["answer"]]
    assert not r["answer"].startswith("Replay:")


def test_speak_event_keeps_replay_prefix(fakes):
    r = pipeline.speak_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on", "source": "replay"})
    assert fakes["spoken"][0].startswith("Replay:")


def test_speak_event_with_nothing_to_say_is_silent(fakes):
    r = pipeline.speak_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "off"})
    assert r["answer"] == "" and fakes["spoken"] == [] and fakes["played"] == []


# ---------------------------------------------------------------- one voice at a time
def test_alarm_interrupts_a_playing_answer_then_speaks(monkeypatch, fakes):
    """The answer is 'playing' (blocked) when an alarm arrives: the answer is stopped, then the alarm plays."""
    order = []
    answer_playing, cut_short = threading.Event(), threading.Event()

    def fake_play(path):
        if path.endswith("1.wav"):                       # first wav = the answer
            order.append("answer start")
            answer_playing.set()
            cut_short.wait(timeout=5)                    # "plays" until stop_playback() is called
            order.append("answer stopped")
        else:
            order.append("alarm played")

    monkeypatch.setattr(pipeline.audio_io, "play_wav", fake_play)
    monkeypatch.setattr(pipeline.audio_io, "stop_playback", cut_short.set)

    t = threading.Thread(target=pipeline.handle_voice_turn, args=("q.wav",))
    t.start()
    assert answer_playing.wait(timeout=5)
    pipeline.speak_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})
    t.join(timeout=5)
    assert order == ["answer start", "answer stopped", "alarm played"]


def test_alarm_does_not_cut_off_another_alarm(monkeypatch, fakes):
    stopped = []
    monkeypatch.setattr(pipeline.audio_io, "stop_playback", lambda: stopped.append(1))
    pipeline.speak_event({"t": "alarm", "id": "flood", "level": "alarm", "state": "on"})
    pipeline.speak_event({"t": "alarm", "id": "heat", "level": "warn", "state": "on"})
    assert stopped == [] and len(fakes["played"]) == 2
