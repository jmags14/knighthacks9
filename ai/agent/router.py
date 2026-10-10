"""The router (AGENT_PLAN 5.3): plain keywords, no LLM. Easy to read and debug.

route(text) -> {"intent", "path", "tools", "topic"}
  path "tool" : answer with a template from live data (P2)
  path "rag"  : not a live-data question; the agent tries canned (P1), then RAG + LLM (P3)

Live-data patterns are deliberately narrow. "What does the risk score mean?" is a guide
question (canned), but "what is the storm risk?" wants today's number.
"""
import re

# (intent, pattern, tools, "not" pattern that cancels the match). First match wins.
INTENTS = [
    ("storm_status",
     r"\b(is|are) (it|the storm|the hurricane|this|that|we)\b.{0,12}\b(over|passing|passed|ending)\b"
     r"|\b(storm|hurricane) (is )?(really |finally |actually )?(over|passing)"
     r"|\b(is|are) (this|it|that|we).{0,12}\beye\b|\bin the eye\b"
     r"|\bsafe (to go |to step )?(outside|out)\b|\b(go|come) outside\b",
     ["get_storm_status", "get_alarms"], None),
    ("storm_risk",
     r"\b(what('s| is)|current|our|the) (storm )?risk( score| level)?( now| right now)?\s*\??$"
     r"|\bhow (high|big|bad) is (the )?(storm )?risk\b|\bstorm risk (now|level)\b"
     r"|\bhow bad (is|will|are)\b|\bis a storm coming\b|\bstorm coming\b",
     ["get_storm_status"], None),
    ("water_days",
     r"\bwater (will |would |is going to )?last\b|\bhow (many days|long).{0,25}\bwater\b"
     r"|\bdays of water\b|\bhow much water (do we|do i|is|we) (have|left)\b|\bwater (do we have|left)\b",
     ["water_calculator"], None),
    ("battery",
     r"\bbox.{0,20}battery\b|\bbattery.{0,20}(box|left|remain|hours)\b|\bpower bank\b"
     r"|\bhow long.{0,25}(box|stormbox|battery).{0,15}(last|run|go)\b|\bbattery (life|level|percent)",
     ["get_battery"], r"\b(phone|cell|laptop|tablet)\b"),
    ("outage",
     r"\bwhen did (the )?(power|electricity)\b|\b(power|electricity) (has been|been|is|was) (out|off)\b"
     r"|\bhow long (has|have|was|were|is).{0,20}(power|electricity|outage)\b"
     r"|\boutage (log|history|duration)\b|\bare we on (battery|wall power)\b",
     ["get_outages"], None),
    # "What's the weather like outside?" The box only measures INSIDE the house, so the answer says so.
    ("weather",
     r"\bweather\b|\bconditions\b|\bhow('s| is| are) (it|things) (looking|going|out)\b"
     r"|\bwhat('s| is) it like (out|outside)\b|\b(what|how)('s| is| are)\b.{0,25}\boutside\b",
     ["get_sensors", "get_storm_status"],
     r"\b(generator|grill|stove|charcoal|food|pets?|dog|cat|car|run|use|keep|store|cook|leave|kit)\b"),
    ("sensors",
     r"\b(temperature|humidity|barometer|wind speed|how windy|rainfall|how much rain)\b"
     r"|\bhow (hot|cold|humid) is it\b|\b(current|what('s| is) the) (air )?pressure\b|\bpressure (reading|now)\b"
     r"|\b(sensor )?readings?\b"
     r"|\bis it (very |really |getting )?(windy|raining|rainy|hot|cold|humid)\b|\bhow (strong|fast) is the wind\b"
     r"|\bhow hard is it raining\b|\bwind right now\b",
     ["get_sensors", "get_storm_status"],        # storm status gives the pressure trend for the answer
     r"\b(fridge|freezer|food|insulin|cook|medicine|water|oven)\b"),
    # Questions about water coming IN. If the flood alarm is on we answer with the P0 flood text;
    # if not, the agent carries on to canned / RAG with topic=flood.
    ("flood_check",
     r"\bwater (is |was )?(on|coming|rising|under|in|entering)\b.{0,15}\b(floor|door|house|home|basement|in)\b"
     r"|\bwater (is )?(ankle|knee)\b|\bflooding (in|inside)\b|\bis (my|the|our) (house|home|basement) flooding\b"
     r"|\bwater on the floor\b|\bankle.deep\b",
     ["get_alarms", "get_sensors"], None),
]

# Topic guess for guide search (first match wins, so order matters: first-aid before flood, etc.)
TOPICS = [
    ("first-aid", r"\b(cut|bleed\w*|wound\w*|injur\w*|tourniquet|bandage)\b"),
    ("medical", r"\b(insulin|medicine|medication|prescription|oxygen|inhaler|dialysis)\b"),
    ("pets", r"\b(dog|cat|pet|pets|puppy|kitten)\b"),
    ("after", r"\b(power line|downed|fallen|after the storm|damaged (home|house)|mosquito\w*)\b"),
    ("air", r"\b(generator|carbon monoxide|co alarm|fumes|gas stove|charcoal|air alarm|smoke)\b"),
    ("food", r"\b(fridge|freezer|refrigerat\w*|chicken|meat|food|milk|eggs|spoil\w*)\b"),
    ("flood", r"\b(flood\w*|ankle|water (is )?(coming|rising|under))\b"),
    ("water", r"\b(purify|bleach|boil\w*|bottled|safe to drink|drinking water|disinfect\w*)\b"),
    ("evacuation", r"\b(evacuat\w*|shelter in place|leave (the )?(house|home|area))\b"),
    ("heat", r"\b(heat|hot|degrees|cool|cooling|overheat\w*|sunstroke)\b"),
    ("storm", r"\b(hurricane|tornado|storm|eye|windows?|surge|wind)\b"),
    ("communication", r"\b(911|phone|text|call|cell|signal)\b"),
    ("kit", r"\b(kit|supplies|pack)\b"),
    ("power", r"\b(power|outage|blackout|breaker|electric\w*)\b"),
    ("stormbox", r"\b(stormbox|risk score|phase|barometer)\b"),
]


def guess_topic(text):
    t = text.lower()
    for topic, pattern in TOPICS:
        if re.search(pattern, t):
            return topic
    return None


# What a weather/sensor question is really about, so the answer can start with it ("Is it windy?" -> wind first).
FOCUS = [
    ("wind", r"\b(wind|windy|winds|gusts?|gusty|breeze)\b"),
    ("rain", r"\b(rain|raining|rainy|rainfall|precipitation)\b"),
    ("pressure", r"\b(pressure|barometer|barometric)\b"),
    ("temperature", r"\b(temperature|temp|hot|cold|warm|humid|humidity|degrees)\b"),
]


def focus_of(text):
    t = text.lower()
    return next((name for name, pattern in FOCUS if re.search(pattern, t)), None)


def route(text):
    t = text.lower().strip()
    for intent, pattern, tools, never in INTENTS:
        if re.search(pattern, t) and not (never and re.search(never, t)):
            return {"intent": intent, "path": "tool", "tools": tools,
                    "topic": "flood" if intent == "flood_check" else None,
                    "focus": focus_of(t) if intent in ("weather", "sensors") else None}
    # Everything else: canned, then guides + LLM. Give the LLM today's readings and alarms.
    return {"intent": "general", "path": "rag", "tools": ["get_sensors", "get_alarms"],
            "topic": guess_topic(t), "focus": None}
