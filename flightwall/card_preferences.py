"""Display-only card choices; these never grant consent or enable a feed."""

CARD_LABELS = {
    'flight_cards_visible': 'Operating flights',
    'trip_cards_visible': 'Upcoming trips',
    'nearby_card_visible': 'Nearby aircraft',
    'weather_card_visible': 'Home weather',
    'radar_card_visible': 'Weather radar',
    'trip_weather_cards_visible': 'Airport weather',
    'airport_alert_cards_visible': 'Airport advisories',
    'custody_card_visible': 'Family outlook',
    'local_visit_cards_visible': 'Local airport visits',
    'dinner_cards_visible': 'Dinner menus',
    'dinner_options_card_visible': 'Dinner ideas',
    'appointment_cards_visible': 'Appointments',
    'flight_watch_cards_visible': 'Tracked flights',
}
CARD_DEFAULTS = {key: True for key in CARD_LABELS}
VIEW_FIELDS = {
    **{view: 'flight_cards_visible' for view in ('details', 'map', 'schedule', 'operating_map', 'flight_status', 'flight_brief')},
    'trip_summary': 'trip_cards_visible', 'trip_map': 'trip_cards_visible',
    'nearby': 'nearby_card_visible', 'weather': 'weather_card_visible',
    'national_radar': 'radar_card_visible', 'trip_weather': 'trip_weather_cards_visible',
    'airport_alerts': 'airport_alert_cards_visible', 'custody_outlook': 'custody_card_visible',
    'today_activities': 'custody_card_visible',
    'local_visit': 'local_visit_cards_visible', 'flight_watch': 'flight_watch_cards_visible',
    'dinner_options': 'dinner_options_card_visible',
}


def card_enabled(view, config, item=None):
    field = VIEW_FIELDS.get(view)
    if view == 'personal' and isinstance(item, dict):
        field = {'dinner': 'dinner_cards_visible', 'appointment': 'appointment_cards_visible'}.get(item.get('kind'))
    return field is None or config.get(field, True) is True
