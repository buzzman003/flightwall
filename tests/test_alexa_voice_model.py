"""Guard the short command forms supported by the existing Alexa handler."""
import json
from pathlib import Path
import re


def test_concise_voice_commands_map_to_the_expected_intents():
    path = Path(__file__).resolve().parents[1] / "integrations/alexa/interaction-model-en-US.json"
    model = json.loads(path.read_text(encoding="utf-8"))["interactionModel"]["languageModel"]
    assert model["invocationName"] == "flight wall"
    expected = {
        "AddDinnerIntent": ("add a dinner card", "add dinner card", "put {Meal} on the dinner card"),
        "StartTimerIntent": ("start a timer for {TimerDuration}", "start timer for {TimerDuration}",
                             "add a timer for {TimerDuration}"),
        "ListTimersIntent": ("list my timers", "what timers do i have"),
        "CancelTimerIntent": ("cancel my {TimerLabel} timer", "stop the {TimerLabel} timer"),
        "TrackFlightIntent": ("track {Airline} {FlightNumber}", "watch {Airline} flight {FlightNumber}",
                              "watch {Airline} {FlightNumber}"),
    }
    intents = {intent["name"]: intent for intent in model["intents"]}
    for name, samples in expected.items():
        intent = intents[name]
        slots = {slot["name"] for slot in intent.get("slots", [])}
        for sample in samples:
            assert sample in intent["samples"]
            assert set(re.findall(r"\{(\w+)\}", sample)) <= slots

    samples = [re.sub(r"\{\w+\}", "{}", line.casefold()) for intent in model["intents"] for line in intent["samples"]]
    assert len(samples) == len(set(samples))
