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

airport-timezones.json maps 7,680 known IATA airport codes to IANA time zone
names for opt-in MiCrew schedule parsing. It retains just the iata and tz
columns for codes present in airports.json with one unambiguous nonempty zone.
Source snapshot: airportsdata v2026.08.03, MIT. The upstream license is included
as LICENSE-airportsdata.txt. An airport without a valid zone is unavailable to
the parser.
https://github.com/mborsetti/airportsdata/blob/fb23038b6fd01e572ce508ac1a077a6e6b3354c5/airportsdata/airports.csv

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
radius. Its private display center is used only in memory and stays outside
releases. Map features are reference geography, not operating data.

GULF COAST NEARBY GEOGRAPHY

gulf-coast.json.gz contains land, water, road and runway vectors in the fixed
public region west=-90.9, south=28, east=-82, north=33.1. The Fort Walton Beach
and Destin 10, 25, 50 and 100 mile presets use this wider map as needed. At 50
miles and above, only major roads and US 98 are drawn. Beyond the regional
coverage, the renderer uses its generalized Natural Earth world map.

Source files retrieved November 4, 2026, Central Time:
* Natural Earth 1:10m land and lakes, tagged v5.1.2 (public domain).
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_land.geojson
  https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_lakes.geojson
* U.S. Census Bureau TIGERweb 2026 primary and secondary roads (public domain).
  https://www.census.gov/data/developers/data-sets/TIGERweb-map-service.html
  https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Transportation/MapServer/layers
* Public-domain OurAirports runway endpoints from airport-runways.json below.

To rebuild, save the two Natural Earth GeoJSON files as nearby-land.geojson
and nearby-lakes.geojson. Query TIGERweb Transportation MapServer layers 2
and 4 and save the two GeoJSON results as nearby-tiger-2.geojson and
nearby-tiger-4.geojson. Use the following query parameters on each layer:

  where=1=1
  geometry=-90.9,28,-82,33.1
  geometryType=esriGeometryEnvelope
  inSR=4326
  outSR=4326
  outFields=MTFCC,NAME,RTTYP
  geometryPrecision=5
  maxAllowableOffset=0.0003
  f=geojson

Run scripts/extract-gulf-coast-map.py --input-dir <source-folder> from the
repository root using the build-only Shapely dependency. The extractor
records source checksums, clips and simplifies the vectors, and saves
the compressed asset. Runtime rendering needs no map service or network
request. The on-screen attribution reflects the geography actually shown.

AIRPORT WEATHER AND RUNWAY REFERENCES (0.4.7)

airport-weather-stations.json retains published four-character airport/weather
identifiers for 8,315 matching bundled IATA airports. Identifiers are sourced,
not formed by guessing a country prefix. Airport weather availability is still
confirmed by the exact station identifier in the provider's response.
airport-runways.json retains 5,537 airports with published open-runway endpoint
coordinates and width. It includes five ATL runways and two VPS runways. These
are display references, not taxiways, clearances, filed routes or navigation.
An airport with missing endpoints has no invented runway geometry.

Source: https://ourairports.com/data/ (public domain), retrieved October 31, 2026.
https://davidmegginson.github.io/ourairports-data/airports.csv
SHA256: 6c600b7821829c2bd672c97b0c1e25b51a12e1e281fbd6fb1b698156b17d6acb
https://davidmegginson.github.io/ourairports-data/runways.csv
SHA256: 64103ae9a363538774f43c9b7b510962ac8f1d89ec976be2c91829622d6edd67
Reproduce with scripts/extract-airport-references.py and these CSVs. Only rows
matching existing airport coordinates survive; closed, malformed, zero-length
or unrelated endpoints are discarded. Raw source CSVs are not distributed.
