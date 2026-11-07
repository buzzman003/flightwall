FLIGHT WALL 0.4.16

Ports can verify and raise an existing Flight Wall window. The running TV
display can reclaim focus from the games menu. Escape ends the display session.
Autostart waits for the userdata mount and a usable menu window before opening
Ports. Existing installations need the verified stable launcher and autostart
updates as well as the signed app update.

Trip weather adds an at-home VPS overnight preview for the remaining legs
through the next local day. It requires a fresh Home report. The VPS visit
shows estimated home arrival 45 minutes after scheduled arrival, and both VPS
cards can use a matching Buzz Brief leave window. Nearby Aircraft can stay in
rotation during duty. A completed operating leg retains its observed gate clock,
and each planned current-leg map appears once per rotation.

Today's Activities hides a timed event 30 minutes after it ends and preserves
explicit Soccer Game and Soccer Practice labels. Personal Delta travel uses
reviewed records in a separate private store. Use travel-import to add or
reconcile a reservation and travel-list to review the stored entries. See
docs/CLI.txt for the accepted fields. The app continues to read calendars only.

FLIGHT WALL 0.4.15

OwnTracks can recover after an outage without an old queued report blocking
new observations. Authenticated expired/older reports and supported non-presence
telemetry are acknowledged and discarded. They do not change Home/Away, renew
freshness or save coordinates. Malformed reports, future observations, wrong
credentials and disabled presence remain errors. The crisp display is unchanged.

FLIGHT WALL 0.4.14

S > Timing sets individual card durations. GLOBAL uses the Display card seconds.
Space pauses rotation, P holds the current card for 45 seconds, and the small
circle beside the deck squares shows the remaining card time. The header shows
PAUSED or the temporary hold countdown.

Trip weather is a graphical day-before preview while Chris has a fresh Home
report. It uses arrival/departure windows, source-dated forecasts and explicit
unknown coverage. Dinner photos use clear presentation resolution above the
LED board and persist with the selected meal. Custom meal images share the
existing dinner allowance. The family outlook marks planned Buzz duty days.

Setup > Helpful connections > Bring TV display forward restores a running
display. The focus command also brings one verified existing X11 window forward.
Fresh installers use it when Ports is selected while the app is already open.
Existing installations require the separately reviewed stable launcher update.
The original pinned recovery helper is retained.

Commute planning keeps a primary and one later VPS-to-ATL backup, both scheduled
to arrive before the exact roster report time. Dated public schedule candidates
can be imported with commute-plan; commute-schedules can query explicitly chosen
Delta flight numbers through the existing FlightAware request allowance. A private
commute_schedule_idents list enables one refresh batch per report/selection/day,
only on the day before travel while Chris is freshly Home. Current verified
same-day plans avoid another lookup. These are planned flights, not proof of
boarding or seat availability, and do not become operating roster legs.

FLIGHT WALL 0.4.13

Flight Brief groups significant FlightAware timing changes, relevant FAA
airport advisories, observed endpoint weather and matched VPS leave-home and
departure times. The provider card uses observed gate and runway events for
taxi phases. Today's Activities marks the current and next timed events locally.

Under Setup > Connections, Chris can opt in to OpenAI flight-brief ordering.
It uses the saved key and at most four small text requests per UTC day. Only
coarse categories are sent. Local ordering works offline and remains available
if the API fails. No new email or Alexa permissions are required.

FLIGHT WALL 0.4.9 - PUBLISHED, VERIFIED AND ACTIVE

Shortens dinner help to one spoken example and adds "set dinner card" as a
simple way to begin. Menu selection accepts numbers 1-4 and matching displayed
meal titles, tied to today's exact generated set. The short greeting, optional
sides/ready-time follow-ups, invocation and scoped voice permissions remain.
Shorter help and numeric aliases work with 0.4.8; selecting by title requires
the matching 0.4.9 backend and its normal next-safe-Ports-launch activation.
Linux passed 1318 tests and 539 subtests with zero skips/failures, plus frozen
graphics and both executable upgrade/rollback gates. Published files passed
independent signature, inventory and source checks. A verified 57-file private
backup and ten protected files preceded GUI-live staging; settings, credentials,
stable wrappers and pinned recovery were preserved. The matching Lambda/model
are deployed. The owner reopened Ports normally; strict-host-key SSH confirmed
healthy active 0.4.9, all existing settings/private files, stable wrappers and
pinned recovery, with no pending update or startup-health flag. One Google
cache changed only through permitted ephemeral refresh. The simulator confirmed
short dinner help and the new meal prompt without changing saved meals. Real
displayed-option selection and first paid daily generation remain field checks.
See docs/RELEASE-0.4.9.txt and docs/VALIDATION.txt for field-check limits.
Release: https://github.com/buzzman003/flightwall/releases/tag/v0.4.9

Current verified active baseline follows.

FLIGHT WALL 0.4.8 - PUBLISHED, VERIFIED AND ACTIVE

Adds four daily pictured meal ideas, dinner sides/ready clocks and tomorrow menus,
animated NOAA radar and S options for card visibility, aircraft radius and
radar area. Defaults preserve existing consent, provider choices and Kids Home
rules. Dinner's display window remains 14:30-19:00 in the configured timezone.
The logical LED board remains 512x256; radar/geography use a clear image inset.
See docs/RELEASE-0.4.8.txt and docs/VALIDATION.txt for checks and limitations.
Signed Linux/native acceptance, independent published-artifact verification
and normal Ports activation passed. Strict-host-key SSH confirmed healthy
0.4.8, preserved settings/private files and unchanged pinned recovery. The
matching Alexa Lambda and voice model are deployed; the simulator completed
the sides/ready-time prompts and cancellation without changing saved meals.
The owner confirmed the Maps controls, paused radar animation and physical Echo
launch/dinner help. A real dinner save with the new follow-ups and the first
eligible paid meal generation remain separate field checks. The private meal
key and approved $3 monthly
feature allowance are configured; no paid food request has run yet.
Release: https://github.com/buzzman003/flightwall/releases/tag/v0.4.8

PREVIOUS RELEASE:
FLIGHT WALL 0.4.7 - PUBLISHED, VERIFIED AND ACTIVE

Adds a position-driven operating map, current-flight focus, optional nearby
origin/destination packets under the shared $5 app budget, decoded airport
weather, national observed radar and FAA airport advisories. Ports logo/video
assets and a rare split-flap reveal are included. The logical LED board stays
512x256, with geographic pixels presented at display resolution.

New network connections default off and DEMO stays offline. Existing settings,
credentials, games and the original pinned recovery helper remain outside the
release. No global Echo timer or native Alexa list access is claimed.
Linux passed 1072 tests and 519 subtests, zero skips/failures, plus frozen
graphics, fresh install, original 0.3.0 and published 0.4.6 upgrade/rollback
gates. Independent signature/inventory/source verification passed. A verified
42-file private backup and nine protected files preceded GUI-live staging.
Settings and pinned recovery were preserved. Ports image/logo/video metadata
and safe frontend reload passed with other game entries/artwork unchanged.
The owner confirmed artwork/video hover playback and reopened Ports.
Strict-host-key SSH verified healthy active 0.4.7, no pending/startup-health
flag, all 42 existing private files, nine protected credentials, five stable
wrappers and the original pinned recovery. All original config values were
preserved before approved feature preferences were separately applied with
another private config backup. New physical context cards and real-flight
coverage remain separate checks. See docs/RELEASE-0.4.7.txt and
docs/VALIDATION.txt. No Alexa companion redeployment is needed.
Release: https://github.com/buzzman003/flightwall/releases/tag/v0.4.7

PREVIOUS RELEASE: FLIGHT WALL 0.4.6 - PUBLISHED, VERIFIED AND ACTIVE

0.4.6 adds left/right previous/next card navigation, 24 brief random retro
reveals and clearer weather clocks. Keyboard arrows, controller D-pad/left
stick and remotes that send arrow keys navigate the current eligible deck.
Settings keep their directional controls. Sensitive and freshness-critical
cards switch immediately; timers and ticker remain live outside reveals.

Weather separates NWS ISSUED from CHECKED, with honest TONIGHT/LOW wording
when the provider no longer supplies a daytime high. Two live fetches occurred
15 minutes and one second apart while the display stayed open; no routine
restart is needed. The crisp no-glow LED preference is preserved.

Linux passed 822 tests and 443 subtests with zero skips/failures. Frozen
OpenGL/software, fresh install, original 0.3.0 and published 0.4.5 executable
upgrades and rollback passed. Public signature, inventories and source verified.
A 41-file private backup and protected-credential/settings checks passed.
The owner reopened Ports and strict-host-key SSH verified active 0.4.6, with
no pending update or startup-health flag. All original settings and private
files, five stable wrappers and the pinned 0.3.0 recovery are preserved. One
Google cache changed only through permitted ephemeral token refresh; one added
config field matches its exact default. Staging did not restart services, games
or the OS. The owner reports that left/right navigation works in both directions
on the TV. The input device type and transition appearance remain unverified.

AeroAPI requires a separate Personal account and private key. Its optional
app request budget is capped at $5/month; that is not a provider-account-wide
spending cap. The release contains no credential; account/key onboarding and
real-provider verification are separate private setup steps. Physical input
device identification and transition appearance checks remain separate from
the owner-reported navigation success. See docs/RELEASE-0.4.6.txt.
Release: https://github.com/buzzman003/flightwall/releases/tag/v0.4.6

PREVIOUS RELEASE: FLIGHT WALL 0.4.5 (HISTORICAL ACTIVATION RECORD)

The signed 0.4.5 release is published, independently verified and active after
the owner's normal Ports relaunch. Linux passed 745 tests and 434 subtests,
zero skips/failures, plus native installation/upgrade/rollback gates. The
41-file private backup and protected-credential checks passed. Activation
verified existing settings, launchers and the original pinned recovery helper;
no pending update or startup-health flag remains.

0.4.5 adds actual-deck progress squares, AM/PM clocks throughout the
TV, automatic 15-minute weather polling and crisp weather icons. Operating
flight status/early-late cards are prepared for AeroAPI, with a shared monthly
app request budget capped at $5. No account/key has been connected; paid
tracking remains off. See docs/RELEASE-0.4.5.txt for release and next-launch status.
No close/reopen is needed for routine weather refresh. Program changes still
activate only at the next safe Ports launch; local source edits are not a
new executable. Previous installed-release evidence follows.

The signed 0.4.4 release is published, independently verified and active on
this owner's Batocera. Linux passed 688 tests and 403 subtests with no skips,
plus native graphics, fresh install, both upgrade paths and rollback. The
43-file private backup, protected credentials, stable wrappers and original
recovery helper verified; existing settings were preserved before the newly
approved Brief selection/leave-time settings were applied separately.

0.4.4 removes airport labels over geographic route lines, adds dated VPS
outbound departures and optional narrowly parsed Buzz leave-home notes, and
shows the dinner snack notice from 17:00. The nearby geographic inset uses
bundled OSM water, roads, US98 and runways at presentation resolution; LED
titles and the 512x256 board remain. No map subscription or runtime map query.
The actual rich-text Brief note passed exact parser/outbound matching, and
the active backend supplies the chosen leave clock and separate departure.
The owner confirmed the detailed map and both dated VPS clocks on the TV.
The new dinner snack panel still needs a separate physical visual check.
Physical Echo opening remains unresolved; no routing repair is claimed.
See docs/RELEASE-0.4.4.txt and docs/VALIDATION.txt for separate evidence.
Release: https://github.com/buzzman003/flightwall/releases/tag/v0.4.4

The prior published/installed release evidence follows:

The signed 0.4.2 Linux release is published and independently verified. Linux
validation passed 645 tests and 373 subtests, with no skips or failures, plus
native graphics, fresh installation, upgrade and rollback checks. The owner's
Batocera has activated 0.4.2 with preservation verified, and the owner confirmed
the cleaner map looks much better. After explicit location-sharing approval,
NWS returned a fresh forecast and ADSB.lol returned a valid empty aircraft
response. The owner confirmed the weather card, and the installed app's timer
list passed automatic five-minute expiry. Visual TV-strip expiry remains unchecked.
See docs/RELEASE-0.4.2.txt for changes and docs/VALIDATION.txt for the separate
source, release and physical-device evidence. Echo opening remains unresolved.
Release: https://github.com/buzzman003/flightwall/releases/tag/v0.4.2

Your calendar-driven flight board and household display, delivered as a
Batocera Port. Initial platform: x86_64 Batocera 42 on an HP Ryzen mini PC
and HDMI TV, also installed and checked on the owner's Batocera 43.1.
Published installer packages contain a frozen Linux executable; no Python
installation or programming is required on Batocera. Use the published
installer or signed update ZIP to install the executable; source edits alone
do not update the app on the TV.

START HERE: docs/INSTALL.txt
Household onboarding, both phones and Google accounts: docs/HOUSEHOLD-SETUP.txt
First offline demo: docs/QUICKSTART.txt
Google Calendar details and Desktop OAuth fallback: docs/CALENDAR.txt
Presence: docs/PRESENCE.txt. Optional previews: docs/MESSAGES.txt
ChatGPT/Codex connection: docs/REMOTE.txt
Signed releases: docs/UPDATES.txt and docs/UPDATE-CHANNEL.txt
Alexa appointments and dinner: docs/ALEXA-SETUP.txt
Optional live/nearby traffic and local weather: docs/FEEDS.txt
Tested behavior and account/device limitations: docs/VALIDATION.txt

The TV draws 512 x 256 logical pixels with an OpenGL LED effect and a
software fallback. A physical P1.25 panel at that resolution would have a
lit area about 25.2 x 12.6 inches. Your TV simulates dots; it is not a physical
fine-pitch LED panel. Settings and private data stay outside app releases.

FLIGHTS FIRST

Choose FC View and optionally Buzz Flight Brief through verified Google
calendar lists. Individual flight details, the planned route map and the
schedule use real event times. During the default one-hour-before, flight,
and one-hour-after window, details/map get 60 seconds before each shorter
household view. Other cards default 15 seconds; all timings are adjustable.
A phone going into airplane mode does not disable flight focus. A roster
does not prove anyone's physical location. Nearby traffic stays secondary.
Actual live positions require a separately configured provider; scheduled
flights and maps work without a paid flight-data account.

Trip previews use an adjustable lead-in (flight_preview_days, default five
elapsed days; the owner's current setting is seven). Once eligible, the
preview shows the next five local calendar days starting with the next leg.
Longer breaks show household views instead of distant flight detail. Trip
cards and maps show a dated range with colors per day and a paged legend.
Version 0.4.2 removes route-number plates from the map, keeping airport
codes and a date/city-pair legend. It also enlarges the custody heading.
A dated range is not a guessed roster rotation. The display uses
the configured household time zone, including its daylight-saving changes.
Default aspect-fit scaling makes the LED dots larger while preserving the
512x256 canvas. Integer scaling remains available in settings.

Labeled TV timers can appear above the normal view; the Flight Wall shopping
list scrolls below it. In 0.4.2, completed timers disappear five minutes after
their deadline, even if the display is using cached state; running timers
are unchanged. Alexa commands require a matching board and skill/Lambda.
Ordinary Alexa timers and its
built-in shopping list do not automatically synchronize with Flight Wall.

Version 0.4.2 adds an optional NWS today/tomorrow forecast and overnight
rain reminder for outdoor cushions. Missing or stale data stays unknown or
stale, never a promise of dry weather. Optional ADSB.lol nearby traffic uses a
local position map with a configurable statute-mile radius, fresh positions
with explicit ground reports filtered out, and tail/type when supplied.
Unknown altitude remains unknown. These public feeds need no FlightAware
subscription. Enabling them discloses their configured query location to the
provider; weather can use a separate coarse point kept in private settings.
Neither feature is enabled by a normal offline DEMO launch. See docs/FEEDS.txt.

HOUSEHOLD SETUP

An authenticated browser page at /setup guides calendar roles, phones,
message filters, Lisa's choices, optional OpenAI API, and trip details.
Use a localhost SSH tunnel or your configured public HTTPS address. Google
passwords are entered only at Google, never into this app. ChatGPT's Google
connector does not transfer its credentials to a separately installed app.

Chris and Lisa can authorize separate Google accounts. If Chris already has
read access to Lisa's shared calendar, he can select it without obtaining
Lisa's password or broader account access. Lisa's own Calendar authorization
is read-only and does not authorize Gmail. Exact Mom/Mom's House variants
mean the children are at home; Dad/Dad's House means they are away. Other
labels, conflicts, missing events and stale data mean Unknown.

OwnTracks can report from both iPhone and Android using separate phone
credentials. The board retains current Home/Away state, not GPS history.
Google sign-in does not provide phone location. Phone background reporting
requires each owner's consent, setup and an actual arrival/departure check.

Dinner cards show 14:30-19:00 by default. When the custody filter is enabled,
dinner appears only during a known Mom interval. Appointments appear on their
scheduled day until the end, default one hour. Local cards do not edit Google.
Use the personal CLI or configure the supplied Alexa custom skill/Lambda.
The Alexa credential can create/cancel cards but cannot read flights or mail.
Generated meal selections can reuse their board picture and email the selected
recipe once. Delivery is off until a local owner configures it in a private
interactive terminal:

  /userdata/roms/ports/.data/flightwall/flightwall recipe-email --sender YOUR_GMAIL --recipient RECIPIENT_EMAIL

Enter a dedicated Gmail app password at the hidden prompt. Do not use an
ordinary sign-in password. This is independent of Calendar and read-only
Gmail consent. Remove the private recipe-mail.json file to stop future sends.
The setup command itself does not send an email.
Use the same launcher with recipe-email-status to see delivery counts.
Accepted means Gmail accepted the submission, not that it reached the inbox.

Chris's short message previews are limited to Airbnb/VRBO/HomeAway sources
and explicitly approved lawyer senders, shown when he is known home. Gmail
is separate read-only consent. "May need reply" is a heuristic, not proof.
Phone notifications need an additional configured automation; an iPhone does
not offer a general third-party inbox or all-notification feed.

Lisa chooses her own selected message sources, private artwork, inspiration,
and optional dated research-source cards. Private cards require Lisa known
home, Chris known away and children known away. Unknown/stale state hides
them. G toggles guest mode, hiding all message previews and Lisa's private
cards; normal flight, appointment and dinner views remain available.
Research cards quote source titles/status metadata from PubMed and
ClinicalTrials.gov with dates and links; they are not treatment advice or
AI-generated medical recommendations. Only a separately enabled design
feature sends Lisa's chosen inspiration topics to the OpenAI API. Neither
mail contents nor medical searches are sent to that feature. An API key is
optional and billed separately from a ChatGPT subscription.

Layover cards can show gaps between matching flight legs. Hotel names are
optional: manually entered details or narrowly labeled Hotel/Layover metadata
from selected Chris flight calendars. Raw descriptions, crew and contacts
are discarded; unsupported hotel formats are not guessed.

REMOTE SETTINGS AND CODE UPDATES

The optional background service exposes four authenticated MCP tools:
get_status, update_settings, refresh_calendar, check_updates. Maintenance
status excludes Lisa's private cards and research. The TV has a separate
admin-authenticated display route. Presence, notifications and Alexa each
have scoped credentials and cannot control the application.

Remote settings persist immediately. Program changes use signed releases:
build, sign, publish, verify, stage, activate at the next safe Ports launch,
with startup rollback. Follow the supplied repository/release workflow.
To opt in to launching at every boot, install the supplied
batocera/flightwall_autostart service file at
/userdata/system/services/flightwall_autostart with mode 755, then run
`batocera-services enable flightwall_autostart`. It launches the normal Ports
entry once per boot. Escape returns to the games menu. Disable future automatic
launches with `batocera-services disable flightwall_autostart`.
Internet connectivity alone does not create a ChatGPT connection or release
host. No Google app OAuth, phone, Tailscale device, Amazon/AWS deployment,
source repository, paid API or actual Batocera machine has been connected
by distributing this package. Complete those sign-ins on your own devices.

DEVELOPMENT

Use Python 3.12 on Linux. From the source directory:

  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements-build.txt
  .venv/bin/python -m pytest -q
  .venv/bin/python -m flightwall --data-dir /tmp/flightwall-dev gui --windowed

Build a new matching version using Docker:

  sh scripts/build-linux.sh

Debian 12/glibc 2.36 provides an older baseline than Batocera 42/glibc 2.40.
See Dockerfile.batocera. Do not replace Batocera's system libraries.

Source license: LICENSE.txt (MIT). Map/airport credits: assets/maps/README.txt.
Dependencies retain their licenses; see THIRD-PARTY.txt and bundled notices.
Independent of The Flight Wall, Batocera, Google, OpenAI, Tailscale and
FlightAware. Your official roster remains the source for operating decisions.
