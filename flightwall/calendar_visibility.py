"""Recognize explicit cancellation labels used on calendar entries."""
import re


_STATUS = r"(?:cancell?ed|inactive)"
_MARKER = rf"(?:\[\s*{_STATUS}\s*\]|\(\s*{_STATUS}\s*\))"
_LEADING = re.compile(rf"^(?:[🚫⛔❌✖✕×]\ufe0f?\s*)*(?:{_MARKER}|{_STATUS}(?=$|[\s:–—/\-]))", re.I)
_TRAILING = re.compile(rf"(?:{_MARKER}|(?:\s[-–—/|]\s*|:\s*){_STATUS})\s*$", re.I)
_INACTIVE_DUTY = re.compile(r"^inactive\s+duty\b", re.I)


def event_is_inactive(event):
    """Honor provider state and affirmative title labels, ignoring prose mentions."""
    status = event.get("status")
    if isinstance(status, str) and re.fullmatch(_STATUS, status.strip(), re.I):
        return True
    summary = event.get("summary")
    if not isinstance(summary, str):
        return False
    title = summary.strip()
    # Inactive Duty is a kind of military duty, and can be an active appointment.
    return bool(_TRAILING.search(title) or (_LEADING.match(title) and not _INACTIVE_DUTY.match(title)))
