OFFLINE MAP DATA

world.json: Made with Natural Earth, 1:110 million scale. Contains country land
outlines and first-order administrative subdivision lines, with coordinates
rounded to 0.001 degree. Natural Earth releases this data to the public domain.
Source files, retrieved 21 October 2026:
https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_admin_0_countries.geojson
https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_admin_1_states_provinces_lines.geojson
Terms: https://www.naturalearthdata.com/about/terms-of-use/

airports.json: 8,798 open small, medium and large airports with three-letter
IATA codes; only city, airport name and coordinates retained. Public-domain
OurAirports data, repackaged by DataHub / datasets. Airport code duplicates
prefer the large-airport entry. Coordinates are display reference data.
Source, retrieved 21 October 2026:
https://github.com/datasets/airport-codes/blob/master/data/airport-codes.csv
Original: https://ourairports.com/data/
OurAirports states: "All data is released to the Public Domain, and comes with
no guarantee of accuracy or fitness for use." The datasets maintainers release
their selection and formatting under the Public Domain Dedication and License:
https://opendatacommons.org/licenses/pddl/1-0/

The renderer's 5 x 7 bitmap alphabet is original project code; no font files or
system fonts are needed. Map screenshots show fictional demonstration flights.
The route line is a great-circle reference, never a filed route or live track.

HIGH-RESOLUTION NEARBY REGIONAL GEOGRAPHY

fort-walton-destin.json contains public OpenStreetMap geography for the fixed
Fort Walton Beach / Destin region, west=-86.82, south=30.28, east=-86.15,
north=30.63. These public region bounds are not derived from any household pin.
The source snapshot is dated 2026-10-31T21:58:46Z, retrieved October 31, 2026
from https://overpass-api.de/api/interpreter using normal Windows HTTPS
certificate validation. No private home or aircraft coordinates were submitted.

Unlike the public-domain world and airport assets above, this regional vector
database is licensed under Open Data Commons Open Database License 1.0:
https://opendatacommons.org/licenses/odbl/1-0/
Attribution: (c) OpenStreetMap contributors.
Copyright/source information: https://www.openstreetmap.org/copyright
The adapted vector database is distributed under ODbL 1.0 in this JSON file;
Flight Wall's MIT source license does not replace its map-data license.

The maintenance-only scripts/extract-nearby-map.py records the fixed query and
prepares the asset. Shapely is a build tool; it is not needed on Batocera.
Use --download --system-curl on Windows if its normal system certificate chain
is needed, or --input with a saved Overpass response. Certificate verification
is never disabled. Runtime map drawing is wholly local, with no tile service,
downloaded-tile archive, geocoding, provider credential or map subscription.

Coastlines are joined and polygonized with the public regional boundary, using
OpenStreetMap's land-on-left direction. Interior coastline gaps fail extraction.
Water areas use closed ways or complete outer/inner relation rings; open water
lines are never closed by guessing. Roads retain only class and US98 marker;
runways retain geometry without airport names, labels or operational details.
The runtime clips geography to its inset and keeps a true circular traffic
radius; any viewport outside this regional asset uses the existing radar map.
Its private display center is used only in memory and stays outside releases.
Map features are reference geography, not current runway/road operating data.
