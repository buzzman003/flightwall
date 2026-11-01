FLIGHT WALL PORTS MEDIA

flightwall-logo.png and flightwall-logo-120.png are exact copies of the owner's
previously approved public branding artwork. No new logo was generated.
flightwall-image.png is a fictional DEMO poster from the actual app renderer.
flightwall-preview.mp4 is a silent 15-second H.264/yuv420p preview, 1024x512,
30fps. All flights, forecast values and timers are fixed synthetic examples,
clearly marked DEMO. No live provider, calendar, private home pin or DATA read.

Art direction: the application's own crisp LED palette/alphabet, no glow.
The reusable deterministic timeline and style are in preview-timeline.json.
Rebuild with the source requirements and a developer FFmpeg executable:
  python scripts/build-ports-preview.py --ffmpeg /path/to/ffmpeg
FFmpeg is a build tool only; no encoder is required by the installed app.

The frozen bundle includes this directory. The installer uses the standalone
stdlib scripts/register-ports-media.py helper to update only Flight Wall's
image/marquee/video tags in Ports/gamelist.xml, backing up prior metadata and
overwritten own-file artwork to private DATA/ports-media-backups. Other entries
remain byte-for-byte unchanged. Existing artwork under other filenames stays
in place. Re-running an unchanged registration creates no further backups.

Use Batocera's normal GAME SETTINGS > UPDATE GAMELISTS at a safe opportunity.
EmulationStation may cache/overwrite external edits; no service/frontend/OS
restart is performed by the media helper. Theme support controls video display.

Primary metadata references checked November 1, 2026 UTC:
https://github.com/batocera-linux/batocera-emulationstation/blob/master/README.md
https://github.com/batocera-linux/batocera-emulationstation/blob/master/es-app/src/MetaData.cpp
https://wiki.batocera.org/menu_tree
