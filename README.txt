FLIGHT WALL 0.4.0 SOURCE CANDIDATE

This checkout contains the next version in development. The last published
installer and the household's installed app remain 0.3.0. A matching Linux
build, Linux validation and signed release are required before these source
changes can be installed. See docs/RELEASE-0.4.0.txt for features and status.

Your calendar-driven flight board and household display, delivered as a
Batocera Port. Initial platform: x86_64 Batocera 42 on an HP Ryzen mini PC
and HDMI TV, also installed and checked on the owner's Batocera 43.1.
Published installer packages contain a frozen Linux executable; no Python
installation or programming is required on Batocera. This source candidate
does not contain a newly built executable.

START HERE: docs/INSTALL.txt
Household onboarding, both phones and Google accounts: docs/HOUSEHOLD-SETUP.txt
First offline demo: docs/QUICKSTART.txt
Google Calendar details and Desktop OAuth fallback: docs/CALENDAR.txt
Presence: docs/PRESENCE.txt. Optional previews: docs/MESSAGES.txt
ChatGPT/Codex connection: docs/REMOTE.txt
Signed releases: docs/UPDATES.txt and docs/UPDATE-CHANNEL.txt
Alexa appointments and dinner: docs/ALEXA-SETUP.txt
Optional live/nearby traffic: docs/FEEDS.txt
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

Version 0.4.0 uses a five-day lead-in for trip previews. Longer breaks show
household views instead of distant flight detail. Upcoming trip cards and
maps show a dated range, with colors per day, numbered legs and a paged
legend. A dated range is not a guessed roster rotation. The display uses
the configured household time zone, including its daylight-saving changes.
Default aspect-fit scaling makes the LED dots larger while preserving the
512x256 canvas. Integer scaling remains available in settings.

Labeled TV timers can appear above the normal view; the Flight Wall shopping
list scrolls below it. Alexa commands for these new features require the
matching 0.4.0 board and updated skill/Lambda. Ordinary Alexa timers and its
built-in shopping list do not automatically synchronize with Flight Wall.

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
