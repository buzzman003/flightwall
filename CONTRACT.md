# Flight Wall implementation contract

0.4.7 candidate: operating focus must not infer phone presence or airborne
state from a calendar. Geographic zoom uses only a fresh, exactly associated
provider position; no synthetic aircraft motion. All paid requests share the
existing monthly/daily ledger; optional nearby route enrichment has a stricter
subcap and yields to operating-flight polls. Unknown or stale data fails closed.
Trip METAR/TAF, public-airport NWS, fixed national NOAA radar and FAA delay feeds
have separate local opt-ins, source clocks and freshness bounds. DEMO remains
offline. Never ship private weather/radar/provider caches. Only geographic/radar
insets render above logical resolution; both display paths preserve brightness
and timer/ticker bounds. Safe Ports metadata registration preserves unrelated
gamelist bytes and original media backups. No Alexa companion change is needed.

0.4.6 published, verified and active after the owner reopened Ports. Strict-host-key
SSH verified healthy 0.4.6, no pending/startup-health flags, all original private
files/config values, protected credentials, stable wrappers and pinned 0.3.0
recovery. One allowed Google ephemeral refresh and one exact-default added config
field were accepted. The owner reports that left/right navigation works in both
directions on the physical TV; the input device type and transition appearance
remain unverified. The release contains no credential. AeroAPI account/key
onboarding and real-provider verification are separate private setup steps.

Left/right navigation operates on the current eligible deck,
wrapping in either direction; settings retain directional adjustment behavior.
Short retro reveals must never extend card privacy or visibility. Discard old
frames on eligibility/config/backend changes, and keep timers/ticker fresh
outside card effects. A removed/private card must disappear immediately.
Weather distinguishes provider-issued time from successful retrieval time.
Missing daytime temperature data is not replaced with an invented high.

Target: Batocera v42 x86_64 HP ProDesk Ryzen 2400GE, 1080p HDMI. Native pygame + optional OpenGL LED postprocessing, logical 512x256. No emulator shader assumption. Python 3.12 development, frozen Linux build. Config/state separate from versioned app. App launcher in Ports. Offline demo works without credentials, with visible DEMO labels. FC View via read-only Google OAuth; no user secrets included in distribution.

Shared data conventions (plain JSON dictionaries):

0.4.5 candidate: GUI-local eligible-deck count/index, AM/PM presentation,
automatic900second weather cadence and source-native summary icons. Provider
operating status stays separate from calendar times and ages every frame.
Exact matched provider-event baselines only; tracking-ended is not confirmed
airline cancellation. Paid operating/watch requests share a durable local$5
nominal-fee monthly cap plus existing daily limit. Provider key/enablement,
monthly limit and usage ledger remain local/private; no paid provider is
connected by this release. See RELEASE-0.4.5.txt for rate expiry/account limits.

0.4.4 published/active integration: the logical board remains512x256. Only nearby
geographic pixels may be presented at TV resolution after the LED pass,
following the same timer/ticker content bounds and brightness in both paths.
The native GUI reads its own saved point locally; never add that point to
display/status/MCP JSON. Bundled regional OSM data is public and ODbL-licensed.
Optional Buzz leave-note parsing retains only timestamp anchors/options from
the explicitly selected secondary source. An exact VPS inbound/report/outbound
match is mandatory. A chosen leave time is separate from scheduled departure;
conflicting notes retain a warning. Dinner's per-frame snack cutoff is17:00
in the configured household zone, within its existing end-exclusive window.

- config keys: `calendar_name` (FC View), `calendar_id` (empty), `airline` (empty until selected), `demo` (true until Google setup), `timezone` (UTC), `rotation_seconds` (15), `logical_width` (512), `logical_height` (256), `brightness` (0.7), `glow` (0.25), `dot_gap` (0.22), `led_effect` (true), `show_deadheads` (true), `takeover_hours` (18), `sleep_start` (23:00), `sleep_end` (06:00), `sleep_enabled` (false), `nearby_enabled` (false), `flightaware_enabled` (false), `flightaware_poll_seconds` (120), `updates_enabled` (false), `update_manifest_url` (empty), `update_public_key` (empty), `update_interval_minutes` (15), `public_url` (empty), `control_port` (8765).
- Each flight: `id`, `number` (string, may have airline prefix), `origin` and `destination` (IATA codes), `start` and `end` (RFC3339 with offset), `deadhead` (bool), optional `report` RFC3339, `source` calendar name. Do not pass crew/raw private descriptions to renderer or public logs.
- Snapshot: `flights` list, `last_sync` RFC3339 or null, `error` sanitized string or null, `source` string, `demo` bool. All dates timezone-aware.
- Renderer state: snapshot plus `now` RFC3339, `view` (`details`, `map`, `schedule`, `nearby`), optional `live` (position details explicitly from real provider; never fabricate a live marker). `render_frame(state, config, size=(512,256)) -> PIL.Image.Image RGB`. `selected_flight` supplied by app or renderer selects upcoming/in-progress flight. Output must label scheduled interpolation as schedule estimate, not live. Offline map geometry bundled.
- Root owns app.py, config.py, main.py, pyproject/requirements, integration, tests and packaging finalization. No agent should modify another agent's files without messaging.
- Default data root: `/userdata/system/configs/flightwall` on Batocera, override `FLIGHTWALL_DATA_DIR`. Install root `/userdata/roms/ports/.data/flightwall`; versioned releases under `releases/<version>`, root `current` symlink, updater stages `pending` for next safe launch. Stable launcher/config kept outside releases.
- Remote endpoint on 127.0.0.1:8765, MCP + OAuth protected endpoints; network exposure only explicitly configured via HTTPS proxy such as Tailscale Serve/Funnel. No arbitrary shell/exec tool. Remote operations via callbacks from main app: status, validated settings updates, calendar refresh, update check. Service may run outside GUI for schedule caching/updater.
- Historical baseline VERSION 0.4.2 is published and independently signature/inventory verified, with Linux/native gates passed. It is active on the owner's Batocera with startup/private-data/recovery preservation verified, and the owner confirmed the cleaner map. Explicitly approved NWS and ADSB.lol connections returned a fresh forecast and a valid empty aircraft response respectively; The owner confirmed the weather card; live storage/API timer expiry passed without cancellation. Visual TV-strip expiry remains unconfirmed. Reliable physical Echo invocation remains unresolved. Development, native-release and physical-device checks remain separately identified in docs/VALIDATION.txt.

Household cards (v0.2.0): PersonalStore persists appointments/dinner in a private
JSON file outside releases, independent of read-only Google. Each card has id,
kind, title, start/end/display_start/display_end RFC3339, timezone, source.
Appointment visible local midnight through end; default duration60minutes.
Dinner visible14:30â€“19:00 for its local date. End is exclusive. Runtime status
includes currently visible personal_items. TV inserts one personal view into
the usual rotation and cycles the selected card on each visit. Real household
cards stay real while flights may still be in demo mode. Config personal_enabled
controls display, alexa_enabled is local-only and defaults false.

Alexa custom skill runs via AWS Lambda with configured SkillID and a dedicated
voice-only token. Public HTTPS proxy required. Voice credential can create/cancel
cards and obtain only timezone/default windows; it cannot read the flight roster,
Google credentials or normal control APIs. Request IDs provide retry deduplication.
No actual Amazon/AWS account is configured by the build environment.

Household integration (v0.3.0): HouseholdCalendars owns verified per-profile
read-only calendar roles and exact custody intervals. Runtime.display_status
contains only currently eligible cards; Runtime.status is maintenance/MCP and
strips Lisa messages, private artwork and health cards. GUI uses the dedicated
static-admin /api/v1/display route and build_rotation individual slots. Recheck
privacy/expiry on every frame, including paused views and backend outages.

Household context uses explicit fresh Home/Away for each adult plus custody
Home/Away. Never infer presence from flights, Google sign-in or missing phone
reports. Lisa private requires LisaHOME ChrisAWAY kidsAWAY and explicit enabled
consent. Guest mode hides all messages plus Lisa private cards. Flight focus
uses actual non-demo flight windows independently of presence: default60s
flight views interleaved with15s regular views, from60min before to60min after.

PresenceStore accepts OwnTracks HTTP Basic or per-person bearer credentials,
retains state only, and expires to Unknown. Notifications use independent
per-profile credentials. MessageFeeds requests approved snippets only, never
attachments/full mail bodies, and enforces Chris rental/lawyer restrictions.
SourceContent retains chosen design/artwork cards and dated PubMed/ClinicalTrials
metadata. Health is not AI-generated. Optional OpenAI design generation sends
only configured interests with store:false and a local daily cap.

The authenticated /setup wizard owns scoped Google Web OAuth + PKCE, separate
Gmail consent, CSRF-protected browser sessions and individual phone pairing.
Desktop CLI OAuth remains available. Credentials, consent, household caches,
images and API keys stay in private DATA outside versioned releases. Exact
home addresses and real calendar IDs must never ship in artifacts.

Version 0.4.0 integration:

- Display remains 512x256 with aspect-fit default and optional integer scaling.
  Timers use a centered top strip, shopping a right-to-left bottom ticker.
  Both animate the full content inset without bypassing per-frame privacy.
  Stored scheduling zones remain intact; all TV times use config.timezone.
- Outside flight focus, trip_summary/trip_map appear only within the configured
  flight_preview_days (default 5). They show the next five calendar days of
  upcoming legs, explicitly dated; no inferred rotation identifier. Three-leg
  legend pages preserve every leg. Demo still shows its labeled flight views.
  Without a near trip, home and eligible household views lead the rotation.
- Custody outlook uses ten local calendar days, exact parsed Mom/Dad intervals,
  local-midnight DST boundaries, and Unknown for conflicts/gaps/stale sync.
  VPS/PNS/ECP visit cards are roster highlights, never presence observations.
  Only an immediate matching operating outbound establishes a VPS duration;
  an intervening deadhead breaks that inference.
- timers.json, flight-watches.json, shopping/ and phone-invites/ belong in DATA.
  Voice routes explicitly allow timers, Flight Wall shopping-list actions and
  creation of public airline/date watches. They never expose private calendars,
  mail, credentials or arbitrary settings. Clearing the list requires consent.
- Public watches share the operating-feed paid request budget and yield to
  operating requests. Disabled/unconfigured/ambiguous/stale results are labeled
  honestly. No provider connection is implied by saving a watch request.
- Shopping alerts require explicit Lisa opt-in, a paired fresh report, verified
  store fences and configured SMS sender. At most one send attempt is reserved
  per America/Chicago day across all stores; uncertain sends are not retried.
  No coordinate history or real contacts ship. Generic maintenance status gets
  only shopping count/visibility; TV and voice list routes have explicit access.
- Lisa invitations use one-time expiring fragment tokens, hashes on disk and a
  separate session/CSRF. Lisa alone consents to phone pairing. Invite sessions
  cannot access admin settings, Chris's state, private cards, or mail. Pairing
  does not imply a verified phone report; background reliability needs a real
  locked-phone outing. No SMS, private cards or Gmail consent is implied.

Version 0.4.1 dinner adapter:
- Today and tomorrow use distinct intents with literal-day phrases and one
  SearchQuery Meal slot. Resolve the calendar day in current board timezone;
  never combine a phrase slot with a Date slot in the same sample utterance.
- Read back the actual saved meal/day briefly. Retain hidden/expired-window and
  uncertain-write warnings, original retry IDs and same-date replacement.
- Recognition/routing is external to the handler and must be checked on real
  devices. Do not claim that model changes fix Alexa's native-announcement route.

Version 0.4.2 integration:
- Completed timers are visible only while now < ends_at + five minutes. Running
  deadlines are unchanged. TimerStore reads and renderer frames both enforce the
  cutoff, including backend outages; retry snapshots never recreate old timers.
- Trip map removes route-number plates/connector strokes. Date colors, airport
  codes, paged date/city-pair legends and honest overflow remain. Trip eligibility
  and scheduled-versus-live claims do not change. flight_preview_days controls
  the elapsed-day lead-in, not the five-calendar-day preview span. Custody keeps
  ten local days and conservative Unknown/mixed-day rules with a larger heading.
- Optional weather_enabled defaults false. Separate weather_latitude/longitude
  default null and are local-only: no remote setter/schema or public config.
  A coarse point may be chosen; the query point is disclosed to NWS. WeatherFeed
  polls independently of calendar/control work, never during DEMO. status is
  coordinate-free and non-networking; weather.json stays in private DATA.
- One weather card joins the normal rotation without weakening flight priority.
  Today/tomorrow use config.timezone. Before 08:00, overnight means the remaining
  current night; otherwise it means the coming 18:00-08:00 window, shortened to
  the remaining hours after 18:00. Boundaries honor DST. A fresh rain mention or
  peak hourly precipitation chance >=20% means rain possible. Only complete,
  fresh coverage can mean low chance; gaps/nulls/overlap/stale data never imply
  dry weather. Recheck forecast age/window/date every frame.
- nearby_radius_miles defaults null, preserving nearby_radius_nm. A non-null
  0.1-250 statute-mile value overrides that legacy radius. ADSB.lol receives the
  private home query point. Exact horizontal-radius filtering retains at most
  forty nearest positions excluding explicit ground reports, aged 0-120 seconds
  from provider snapshot time minus seen_pos. Unknown altitude stays unknown. No extrapolation, guessed routes or inferred airports.
  Optional tail/type are only reported values. Recheck freshness each frame.
- A completed nearby query must match current enablement, pin and radius before
  display. Admin TV state retains aircraft positions and home-relative vectors;
  maintenance status removes absolute aircraft coordinates when those vectors
  are present. Raw home/weather pins never appear in public status or releases.
- The 0.4.2 native gate passed original 0.3.0 -> 0.4.2 and original
  0.3.0 -> published 0.4.1 -> 0.4.2, including failed-start rollback to 0.4.1.
  Keep the original pinned 0.3.0 helper, trusted key, version floor, all private
  fixtures and the full tested-source/frozen-bundle/signature binding intact.
