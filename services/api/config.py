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

# Bound expensive work.
MAX_ROI_KM2 = float(os.environ.get("TUNDRA_MAX_ROI_KM2", "2500"))

os.makedirs(COMPOSITE_DIR, exist_ok=True)
