"""Small original type-matched silhouettes, with no network or photo cache.

These intentionally simple top views carry no airline, livery or tail claim.
Only explicitly supported received ICAO types get artwork; unknown types keep
their text rather than borrowing a different aircraft's silhouette.

Shape references (not copied artwork):
https://www.aircraft.airbus.com/en/aircraft/a350/a350-900
https://cessna.txtav.com/en/piston/cessna-skyhawk
https://beechcraft.txtav.com/en/king-air-360
https://www.deutscheaircraft.com/products/d328-turboprop
"""
from functools import lru_cache

from PIL import Image, ImageDraw


@lru_cache(maxsize=4)
def _thumbnail(code):
    # Span/length proportions and the visible propulsion layout distinguish
    # these compact icons. They are illustrations, never aircraft photographs.
    span_ratio, wing_kind, propulsion = {
        "C172": (1.33, "straight", "single_prop"),
        "B350": (1.24, "tapered", "twin_prop"),
        "D328": (1.01, "straight", "twin_prop"),
        "A359": (.97, "swept", "twin_jet"),
    }[code]
    image = Image.new("RGBA", (38, 30))
    draw = ImageDraw.Draw(image)
    white, edge = (225, 237, 239, 255), (91, 151, 172, 255)
    length = min(27, 34 / span_ratio)
    span = length * span_ratio
    cx, nose, tail = 18.5, (30 - length) / 2, (30 + length) / 2
    wing_y = nose + length * .43
    half = span / 2
    if wing_kind == "swept":
        wing = [(cx-1, wing_y-2), (cx-half, wing_y+6), (cx-half, wing_y+8),
                (cx-1, wing_y+2), (cx+1, wing_y+2), (cx+half, wing_y+8),
                (cx+half, wing_y+6), (cx+1, wing_y-2)]
    else:
        taper = 2 if wing_kind == "tapered" else 0
        wing = [(cx-half, wing_y+taper), (cx-half, wing_y+3),
                (cx+half, wing_y+3), (cx+half, wing_y+taper),
                (cx+2, wing_y-1), (cx-2, wing_y-1)]
    draw.polygon(wing, fill=white)
    tail_half = span * (.2 if code == "C172" else .23)
    draw.polygon([(cx-1, tail-5), (cx-tail_half, tail-2), (cx-tail_half, tail),
                  (cx+tail_half, tail), (cx+tail_half, tail-2), (cx+1, tail-5)], fill=white)
    body_half = 1.5 if code == "C172" else 2
    draw.polygon([(cx, nose), (cx-body_half, nose+3), (cx-body_half, tail-5),
                  (cx, tail), (cx+body_half, tail-5), (cx+body_half, nose+3)], fill=white)
    draw.line((cx, nose+3, cx, nose+6), fill=edge)
    if propulsion == "single_prop":
        draw.line((cx-4, nose+1, cx+4, nose+1), fill=edge)
    else:
        for side in (-1, 1):
            engine_x = cx + side * span * .23
            engine_y = wing_y-1 if propulsion == "twin_prop" else wing_y+3
            draw.rectangle((engine_x-1, engine_y-2, engine_x+1, engine_y+4), fill=edge)
            if propulsion == "twin_prop":
                draw.line((engine_x-4, engine_y-2, engine_x+4, engine_y-2), fill=white)
    return image


def thumbnail(aircraft_type):
    if aircraft_type not in {"C172", "B350", "D328", "A359"}:
        return None
    return _thumbnail(aircraft_type)
