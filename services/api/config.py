"""Runtime configuration for the Tundra portal API (env-overridable)."""

import os

# Repo root (two levels up from this file).
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Data locations (the repo `data` symlink points at /mnt/memory/Tundra).
DATA_DIR = os.environ.get("TUNDRA_DATA", os.path.join(ROOT, "data"))
FOOTPRINTS_GPKG = os.environ.get(
    "TUNDRA_FOOTPRINTS",
    os.path.join(ROOT, "map", "Yamal-Nenets", "map_footprints_wgs84.gpkg"),
)
HISTORIC_COG_DIR = os.environ.get(
    "TUNDRA_HISTORIC_COG", os.path.join(DATA_DIR, "historic_cog", "Yamal-Nenets"))
CACHE_DIR = os.environ.get("TUNDRA_CACHE", os.path.join(DATA_DIR, "cache"))
COMPOSITE_DIR = os.path.join(CACHE_DIR, "composites")

# Auth: require this key in the X-API-Key header. Empty disables auth (dev only).
API_KEY = os.environ.get("TUNDRA_API_KEY", "")

# URL prefix the API is reverse-proxied under (e.g. "/tundra"). Prepended to the
# tile-URL templates handed to the frontend so the browser hits the proxied path.
# Empty when served at the site root (dev / standalone).
URL_PREFIX = os.environ.get("TUNDRA_URL_PREFIX", "").rstrip("/")

# Bound expensive work.
MAX_ROI_KM2 = float(os.environ.get("TUNDRA_MAX_ROI_KM2", "2500"))
# Safety ceiling on satellite time-steps a single request may span (annual/
# seasonal/monthly cadence). Each step is a separate STAC search + composite (and
# a SAM pass, for time-series segmentation), so this only guards against a
# pathological request — it is set high enough to cover a full monthly archive
# (~50 yr). The UI warns the user well before this; it is not a normal-use limit.
MAX_SATELLITE_FRAMES = int(os.environ.get("TUNDRA_MAX_SAT_FRAMES", "600"))

os.makedirs(COMPOSITE_DIR, exist_ok=True)
