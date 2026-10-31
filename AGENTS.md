# Flight Wall contributor guide

This is a native x86_64 Batocera 42 Port, built for an HP Ryzen mini PC and
HDMI TV. Read README.txt and CONTRACT.md for app behavior and storage paths.

Keep the normal first launch fully offline and visibly marked DEMO. Render
at 512 x 256 logical pixels. The app owns its OpenGL LED effect; it does not
use an emulator or RetroArch shader. Preserve the software display fallback.
The nearby geographic inset may render at presentation resolution after the
LED pass; the board/title/timer layout remains 512x256. Both display paths
must apply brightness and the same timer/ticker inset to that map layer.

Google Calendar access is read-only and independent of ChatGPT's calendar
connector. Only cache parsed individual flight titles/times and report times.
Never store, ship or log full descriptions, crew information or hotel contacts.
Owner-requested Buzz leave-home notes may be read transiently with the separate
brief_leave_times_enabled opt-in. Keep only narrowly labeled timestamps tied
to an exact VPS arrival/report/outbound match. Preserve conflict warnings;
never guess a leave time or silently equate it to scheduled departure.
The owner explicitly requested hotel/layover names in v0.3.0: optional hotel
mode may extract only labeled hotel metadata from flight layover entries or
accept owner-entered hotel details. Keep it off until enabled; never retain
the underlying description, crew lists, phone numbers or other unrelated data.
Do not guess unsupported roster formats. Schedule times are not live tracking.

Household appointment/dinner cards are stored separately in personal.json.
Appointment display_end equals the appointment's actual end: default one hour
from its start, with no extra grace period. Dinner uses the configured daily
window, default14:30–19:00. Keep timezone/DST validation, request deduplication,
same-date meal replacement, and end-exclusive visibility. Personal cards join
the existing rotation; they do not replace the operating-flight views.
The Alexa Lambda bridge has a dedicated voice-only bearer token, a pinned
SkillID, and bounded HTTPS requests. Never reuse the control token or expose
flight/calendar data through the voice-only routes. Amazon account setup and
live speech recognition still require the owner's real account/devices.

Credentials, calendar cache and mutable config stay in the private data
directory, outside releases. Never commit any user's credentials or calendar.
Remote settings have an explicit allowlist. Keep trust keys, remote origin,
calendar authentication and arbitrary command execution out of remote tools.

Household presence requires an explicit, fresh observation from each opted-in
phone. A roster, Google sign-in or missing report never proves Home or Away.
Lisa's private cards require Lisa Home, Chris Away and children Away, with all
three states known and current. Recheck those conditions every displayed frame,
even during pause, expiry or backend outage. Guest mode hides all messages and
Lisa's private cards. Maintenance status/MCP must exclude Lisa's mail, private
artwork and health cards; the TV uses its separate admin-only display route.
Keep Google Calendar and Gmail consent separate, and presence, notification,
Alexa and administrator credentials isolated. Retain current presence state,
not GPS history. Never include actual home pins, calendar IDs or credentials
in a release. Research cards require source links and dates; do not invent
clinical news or use the design-generation feature for medical conclusions.

Use signed Ed25519 manifests for code updates. Preserve archive safety checks,
the pinned known-good recovery helper, monotonic version floor, next-launch
activation, and startup rollback. Never change a user's pinned public key
through a release. Releases must not include private signing material.

Run `.venv/bin/python -m pytest -q`. Display tests use SDL offscreen OpenGL;
socket tests need local loopback permission in restricted environments.
Run `sh scripts/build-linux.sh` for an older-glibc compatible executable.
Direct requirements belong in requirements.txt. Test/build tools belong in
requirements-build.txt and must not become required on Batocera.

For any code update intended to ship, raise `flightwall.__version__` and the
root `VERSION` file above the last published release; keep them identical.
Build/sign/publish using the configured workflow
only after the relevant tests pass. Do not reuse a release version. Code
changes take effect at the next safe Ports launch; settings save immediately.

Do not claim physical Batocera, Google sign-in, real paid API or live ChatGPT
deployment checks without performing them. Record practical setup limitations
in docs/VALIDATION.txt. Keep novice-facing install steps accurate and executable.
