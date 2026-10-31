# Flight Wall

A native x86_64 Batocera Ports application that turns a TV into a flight board and household display. It draws a 512 × 256 LED-style canvas with its own OpenGL effect and a software fallback. First launch works offline with clearly labeled fictional **DEMO** flights.

This repository contains **0.4.0 source**. Install only a published Linux package from the [Releases page](https://github.com/buzzman003/flightwall/releases); source edits are not an installable update. Each signed release includes its Linux test and native installation/upgrade validation reports. See [features and release gates](docs/RELEASE-0.4.0.txt) and [validation and limitations](docs/VALIDATION.txt).

The board prioritizes scheduled operating flights around departures and arrivals. The 0.4.0 candidate adds dated trip previews, a ten-day custody outlook, persistent timers and a shopping ticker. Optional calendar, phone-presence, Alexa and flight-data integrations each require their own configuration and consent. Calendar schedules are not live tracking.

Private credentials, calendars and mutable settings stay outside application releases in `/userdata/system/configs/flightwall`. Google Calendar access is read-only. Presence requires fresh opted-in phone reports; unknown or stale state hides private cards. No household accounts or credentials are supplied by this repository.

- [Full application guide](README.txt) · [First installation](docs/INSTALL.txt) · [Offline quick start](docs/QUICKSTART.txt)
- [Household setup](docs/HOUSEHOLD-SETUP.txt) · [Alexa setup](docs/ALEXA-SETUP.txt)
- [Signed update channel](docs/UPDATE-CHANNEL.txt) · [Update format and recovery](docs/UPDATES.txt)
- [Contributor instructions](AGENTS.md) · [Behavior and storage contract](CONTRACT.md)

Releases use Ed25519 signatures. Initial trust in a maintainer's public key is a manual local decision; an update never replaces that pinned key. Keep the original installer and recovery helper. The update ZIP is for an existing installation, not a first-install package.

For development, use Python 3.12 on Linux, install `requirements-build.txt` in a virtual environment, then run `.venv/bin/python -m pytest -q`. Build the Linux executable with `sh scripts/build-linux.sh`; Windows source edits alone do not produce it. See [README.txt](README.txt) for the full workflow.

[MIT license](LICENSE.txt). Map and dependency credits are in [assets/maps/README.txt](assets/maps/README.txt) and [THIRD-PARTY.txt](THIRD-PARTY.txt). Flight Wall is independent of Batocera, Google, Amazon, OpenAI and flight-data providers.
