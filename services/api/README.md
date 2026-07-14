# Tundra Portal API (Phase 2)

FastAPI service tying together the datum-corrected historic map footprints/COGs
(Phase 1) with a satellite imagery layer, SAM lake segmentation, and lake
morphometrics. titiler is mounted for dynamic COG tiles.

## Run (dev)

From the repo root, in the project virtualenv (`pip install -r requirements.txt`;
see the top-level [README](../../README.md)):

```bash
export TUNDRA_API_KEY=changeme          # omit/empty to disable auth (dev only)
uvicorn services.api.main:app --host 0.0.0.0 --port 8000
```

Interactive docs at `http://localhost:8000/docs`.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | liveness + data presence |
| GET  | `/footprints?bbox=lon0,lat0,lon1,lat1[&year_min&year_max]` | historic sheet footprints (GeoJSON) with `datum_status`, COG availability |
| POST | `/roi/extract` | build/reuse a cloud-free composite COG for an ROI + list overlapping historic sheets with tile URLs |
| POST | `/segment` | async lake-segmentation job (SAM, GPU-serialised) |
| GET  | `/jobs/{id}` | job status + result (metrics summary + per-lake GeoJSON) |
| GET  | `/tiles/cog/...` | titiler dynamic COG tiles (`/tiles/cog/tiles/WebMercatorQuad/{z}/{x}/{y}?url=<cog>`) |

Auth: send `X-API-Key: <TUNDRA_API_KEY>` when the key is configured.

## Config (env vars)

`TUNDRA_API_KEY`, `TUNDRA_DATA`, `TUNDRA_FOOTPRINTS`, `TUNDRA_HISTORIC_COG`,
`TUNDRA_CACHE`, `TUNDRA_MAX_ROI_KM2`, `TUNDRA_SAM_DIR`. Defaults resolve to the
repo layout (`map/…`, `data/historic_cog/…`).

## Notes

* Composites are cached under `data/cache/composites/` keyed by ROI+date+sensor.
* SAM `vit_h` weights auto-download to `data/models/sam/` on first use (~2.5 GB).
* Docker packaging (api + tiler + web) lands with the Phase 3 frontend so the
  whole stack comes up via one `docker compose up`.
