"""Optional editorial focus for the flight brief, using only coarse categories."""
from __future__ import annotations

import json

from .config import private_json
from .dinner_options import request_json


FOCUS_CODES = frozenset({"airport_delay", "arrival_change", "departure_change", "weather", "leave_time"})
DAILY_LIMIT = 4


def _candidates(codes):
    return tuple(sorted(FOCUS_CODES.intersection(codes)))


def _payload(codes):
    schema = {"type": "object", "additionalProperties": False, "required": ["focus"],
              "properties": {"focus": {"type": "string", "enum": list(codes)}}}
    return {
        "model": "gpt-5-mini", "store": False, "max_output_tokens": 256,
        "reasoning": {"effort": "minimal"},
        "instructions": ("Choose the one household travel update worth reading first. "
                         "Prioritize a change to when the family should expect someone home or a confirmed leave time. "
                         "An airport advisory or a weather observation is context only. "
                         "Return only one of the supplied categories. Do not produce advice or text."),
        "input": json.dumps({"available_categories": codes}),
        "text": {"format": {"type": "json_schema", "name": "flight_brief_focus", "strict": True, "schema": schema}},
    }


def _parse(response, codes):
    if response.get("status") != "completed" or response.get("error") or response.get("incomplete_details"):
        return None
    output = response.get("output")
    if not isinstance(output, list) or len(output) > 4:
        return None
    messages = [item for item in output if isinstance(item, dict) and item.get("type") != "reasoning"]
    if len(messages) != 1 or (messages[0].get("type"), messages[0].get("role")) != ("message", "assistant"):
        return None
    parts = messages[0].get("content")
    if not isinstance(parts, list) or len(parts) != 1 or not isinstance(parts[0], dict):
        return None
    text = parts[0].get("text")
    if parts[0].get("type") != "output_text" or not isinstance(text, str) or len(text) > 100:
        return None
    selected = json.loads(text)
    if isinstance(selected, dict) and set(selected) == {"focus"} and selected["focus"] in codes:
        return selected["focus"]
    return None


class BriefFocus:
    def __init__(self, data_dir, config_getter, private_lock, read_key):
        self.path = data_dir / "brief-focus-usage.json"
        self.config_getter = config_getter
        self.private_lock = private_lock
        self.read_key = read_key
        self._decisions = {}

    def enabled(self):
        config = self.config_getter()
        return (config.get("brief_focus_enabled") is True and config.get("demo") is False
                and config.get("guest_mode") is False and config.get("flight_cards_visible") is True)

    def selection(self, codes, day):
        return self._decisions.get((day, _candidates(codes))) if self.enabled() else None

    def _reserve(self, day, codes):
        with self.private_lock():
            try:
                if self.path.is_symlink() or self.path.stat().st_size > 2048:
                    return False
                attempts = json.loads(self.path.read_text(encoding="ascii"))
            except FileNotFoundError:
                attempts = {"day": day, "categories": []}
            if (not isinstance(attempts, dict) or set(attempts) != {"day", "categories"}
                    or not isinstance(attempts["day"], str) or attempts["day"] > day
                    or not isinstance(attempts["categories"], list) or len(attempts["categories"]) > DAILY_LIMIT
                    or any(not isinstance(row, list) or not all(isinstance(item, str) for item in row)
                           or not 2 <= len(row) <= len(FOCUS_CODES) or list(_candidates(row)) != row
                           for row in attempts["categories"])):
                return False
            if attempts["day"] < day:
                attempts = {"day": day, "categories": []}
            rows = attempts["categories"]
            if list(codes) in rows or len(rows) >= DAILY_LIMIT:
                return False
            rows.append(list(codes))
            private_json(self.path, attempts)
            return True

    def poll(self, codes, day):
        codes = _candidates(codes)
        if len(codes) < 2 or not self.enabled():
            return
        self._decisions = {key: choice for key, choice in self._decisions.items() if key[0] == day}
        try:
            key = self.read_key()
            if not self._reserve(day, codes) or not self.enabled():
                return
            choice = _parse(request_json("/v1/responses", key, _payload(codes)), codes)
        except (OSError, ValueError, TypeError):
            return  # Local ordering stays available, even on refusal or failure.
        if choice and self.enabled():
            self._decisions[day, codes] = choice
