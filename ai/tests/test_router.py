"""Router: keywords -> intent / path / topic. No LLM, no Ollama."""
import pytest

from ai.agent import router


@pytest.mark.parametrize("text,intent", [
    ("Is the storm over?", "storm_status"),
    ("How do I know if the storm is really over?", "storm_status"),
    ("Is this the eye?", "storm_status"),
    ("Is it safe to go outside?", "storm_status"),
    ("What is the storm risk?", "storm_risk"),
    ("How bad is it?", "storm_risk"),
    ("How long will our water last?", "water_days"),
    ("How many days of water do we have?", "water_days"),
    ("How much battery is left?", "battery"),
    ("How long will the power bank run the box?", "battery"),
    ("When did the power go out?", "outage"),
    ("How long has the power been out?", "outage"),
    ("What's the temperature?", "sensors"),
    ("What is the humidity?", "sensors"),
    ("Water is coming under the door, what should I do?", "flood_check"),
    ("Water is ankle-deep, what now?", "flood_check"),
])
def test_live_data_questions_go_to_tools(text, intent):
    r = router.route(text)
    assert r["intent"] == intent
    assert r["path"] == "tool"


@pytest.mark.parametrize("text", [
    "What does the storm risk score mean?",          # explains the feature: a guide question
    "My phone is at 10%, how do I save battery?",    # phone battery, not the box battery
    "What temperature should the fridge be?",        # food question, not a sensor reading
    "Should I open windows to equalize pressure?",   # not a pressure reading
    "What do I do if the power goes out?",
    "How do I make water safe to drink?",
    "Is it safe to walk through flood water?",       # flood advice, not "is my house flooding"
    "What's the capital of France?",
    "What is the eye of a hurricane?",
])
def test_guide_questions_do_not_go_to_tools(text):
    r = router.route(text)
    assert r["path"] == "rag"
    assert r["intent"] == "general"
    assert r["tools"] == ["get_sensors", "get_alarms"]   # context for the LLM


@pytest.mark.parametrize("text,topic", [
    ("Someone has a deep cut, what do I do?", "first-aid"),
    ("Can I run a generator in the garage?", "air"),
    ("Is the chicken in the fridge safe?", "food"),
    ("How do I keep insulin cool?", "medical"),
    ("What should I do with my dog?", "pets"),
    ("How do I purify water with bleach?", "water"),
    ("When should we evacuate?", "evacuation"),
    ("A power line is down in the street", "after"),
    ("It's 95 degrees inside", "heat"),
    ("What's the capital of France?", None),
])
def test_topic_guess(text, topic):
    assert router.guess_topic(text) == topic


def test_flood_check_sets_flood_topic():
    assert router.route("Water is coming under the door")["topic"] == "flood"


def test_router_is_case_insensitive():
    assert router.route("IS THE STORM OVER?")["intent"] == "storm_status"
