DINNER COMMAND UPDATE (0.4.1)

After installing the matching adapter/model, candidate direct forms are:
  "Alexa, ask Flight Wall to set dinner today to chicken teriyaki."
  "Alexa, ask Flight Wall to set dinner tomorrow to tacos and rice."
Inside the confirmed skill session, use "dinner today is chicken teriyaki"
or "dinner tomorrow is tacos and rice". Alexa reads the saved meal/day briefly.
For another date, say "set dinner for Friday", then provide the meal.

These phrases require live recognition checks. The owner's Alexa has sometimes
routed direct dinner requests into native announcements, including an "ask"
form. Do not treat those replies as saved Flight Wall cards. "Let the kids know"
is not a registered variable-content shortcut. Check for the custom welcome and
actual saved card; see VALIDATION.txt for current evidence and remaining tests.
The app's dinner window/custody rules remain unchanged.

Alexa custom-skill cloud adapter for one household Flight Wall board.

Runtime: Python 3.12 or newer. No pip dependencies. Handler:
  lambda_function.lambda_handler

Upload lambda_function.py to your AWS Lambda function. Import the interaction
model JSON into the Alexa Developer Console's English (US) JSON Editor.
Follow docs/ALEXA-SETUP.txt for accounts, Skill ID verification, the dedicated
board token, HTTPS exposure, and test-device setup. Nothing is deployed by
these files. Keep the skill in development for your own Amazon account.

Environment variables required in Lambda:
  ALEXA_SKILL_ID            Exact amzn1.ask.skill... ID of your custom skill
  FLIGHTWALL_BASE_URL       Public HTTPS origin, without a path/query
  FLIGHTWALL_ALEXA_TOKEN    Dedicated token from the board's Alexa setup command

The permission JSON is a template illustrating the Lambda RESOURCE policy;
replace every YOUR/REPLACE field. It is not an execution-role policy. The
recommended console setup adds an Alexa Skills Kit trigger with Skill ID
verification enabled. Do not add a public function URL or a broad second
Alexa trigger, which would defeat the skill restriction.

Voice examples:
  Alexa, open Flight Wall skill.
  (The skill says: Flight Wall. What would you like?)
  Start a pasta timer for five minutes.
  List timers.
  Cancel the pasta timer.
  Track Delta flight one hundred twenty three on Friday.
  (Confirm the airline, flight number and date, then say yes.)
  Alexa, tell flight wall we are having teriyaki chicken for dinner.
  Alexa, tell flight wall add a dentist appointment on Friday at nine A.M.
  Alexa, tell flight wall add an appointment called annual eye checkup.
  (Then say: on Friday; at three P.M.; yes.)

Dinners without an explicit date use today's date in the board's configured
time zone. The board's dinner window defaults to 14:30 through 19:00. To select
another day first say, set dinner for tomorrow, then provide the meal.
Appointments require a complete date/time and an explicit yes after Alexa
reads back their title, date, time, duration, and time zone. A new entry is
personal board data, not a Google Calendar write. Default appointment duration
comes from the board. The board displays an appointment from local midnight
on that day until the appointment ends. With the default one-hour duration,
an 11:25 A.M. appointment displays from midnight through 12:25 P.M.

AMAZON.SearchQuery cannot share a sample utterance with any other slot, and
Amazon requires a reused slot name to have the same type across all intents.
Both appointment Title slots therefore use APPOINTMENT_TITLE. Unlisted titles
may be recognized, but this is not unrestricted dictation. Test your own phrases
in the Developer Console. This adapter is not a general natural-language or
medical-assistant model. To change an entry before saving, say, change the
date to Friday; change the time to three P.M.; or, change the duration to one
hour. Say no to cancel before the write.

Launch is intentionally brief. Say help for the available commands, or dinner
help for meal instructions. The generic fallback does not repeat a long menu.

Timers are persisted Flight Wall countdowns, with labels of 1-80 characters
and whole-second durations from 1 second to 24 hours. Up to five are displayed.
Finished timers remain DONE until dismissed; a new timer can replace the oldest
finished one when all five display positions are occupied. Five running timers
must be reduced before adding another. List timers announces their numbers;
cancel timer number two can disambiguate duplicate labels. Timer deadlines
survive service restarts and device reboots; the device clock must be correct.
These board timers do not import ordinary Alexa system timers, create Echo
alarms, or promise an audible/proactive notification. Alexa's Timers API only
lists timers created by the calling skill; this adapter does not call that API
or request its permission. Ordinary "Alexa, set a timer" still uses Alexa itself.

Flight requests use an airline/code, flight number and explicit date. Missing
or ambiguous values are requested rather than guessed. Common mappings include
Delta DL, American AA, United UA, Allegiant G4 and Frontier F9. G9 is Air Arabia,
not Allegiant. A watch request adds a separate public flight to the board; it
does not disclose or modify the owner's operating-calendar flights. If live
data is not configured, Alexa says so; a saved watch is not proof of live data.
Up to five requests for today through fourteen days ahead use the board's
timezone, saved with the request. Past requested days disappear from rotation.
The optional paid FlightAware feed shares the existing daily cap and checks
operating flights first. Its lookup endpoint accepts only two days ahead;
farther requests wait locally without a query. Exact airline/day matching is
required; multiple legs or further result pages are ambiguous. Schedule data
is distinct from a fresh live position. No provider result is guaranteed.

The board owns its own shopping list, separate from Amazon's native lists.
After opening the skill, say shopping list; add olives; remove olives; or
clear the shopping list. Clearing requires an explicit yes. Show shopping list
keeps its TV ticker visible; read shopping list only reads the list. Up to 100
items of 80 characters are supported; Alexa reads the first ten and a remaining
count. Try the explicit one-shot form, Alexa, ask Flight Wall to add olives.
The alternate phrase Alexa, add olives to Flight Wall is a live routing test
candidate, not a promised override of Alexa's native shopping-list command.
New source utterances still require an Amazon model build and live testing.

Timers, shopping lists and flight watches require the matching new board executable and cloud
adapter/model. Editing this source does not change an installed 0.3.0 board or
deployed Lambda. Use the tested, versioned, signed release workflow and deploy
the matching Lambda/model deliberately; never silently upgrade production.

HTTP calls are bounded, use normal HTTPS certificate checks, reject redirects,
and never log request events/titles. A timed-out write may already have saved;
say yes within the same session to retry its original idempotency ID safely,
or check the board. Starting a completely new voice request creates a new ID.

Official references checked 2026-10-31:
  https://developer.amazon.com/en-US/docs/alexa/custom-skills/slot-type-reference.html
  https://developer.amazon.com/en-US/docs/alexa/custom-skills/host-a-custom-skill-as-an-aws-lambda-function.html
  https://developer.amazon.com/en-US/docs/alexa/custom-skills/test-and-debug-a-custom-skill.html
  https://developer.amazon.com/en-US/docs/alexa/custom-skills/choose-the-invocation-name-for-a-custom-skill.html
  https://docs.aws.amazon.com/service-authorization/latest/reference/list_lambda.html
  https://developer.amazon.com/en-US/docs/alexa/custom-skills/create-the-interaction-model-for-your-skill.html#intent-and-slot-name-requirements
  https://developer.amazon.com/en-US/docs/alexa/smapi/alexa-timers-api-reference.html#list-timers
  https://www.flightaware.com/commercial/aeroapi/resources/aeroapi-openapi.yml
