# OAM Cloud-Native API Mirror

## Overview
Static mirror of the OpenAerialMap catalog API. A daily ETL snapshots the entire OAM `/meta` endpoint into static files on S3, served via CDN. No runtime server, no database queries at read time.

Same pattern as [hot-tm-cn-api](https://github.com/cgiovando/hot-tm-cn-api) (HOT Tasking Manager mirror).

## Architecture
```
OAM API (/meta) → GitHub Actions (daily cron) → etl.py → S3 (public)
                                                   │
                                              tippecanoe
                                                   │
                                              PMTiles
```

## Tech Stack
- **ETL**: Python 3.11, boto3, requests
- **Vector tiles**: tippecanoe → PMTiles
- **Storage**: AWS S3 (or S3-compatible via `S3_ENDPOINT_URL`)
- **CI/CD**: GitHub Actions (daily cron + manual trigger)

## Key Files
- `etl.py` — Main ETL script (~270 lines)
  - `S3Client` — S3/S3-compatible upload/download
  - `OAMApiClient` — Paginates `/meta?page={n}&limit=100`
  - `StateManager` — Tracks `uploaded_at` per image for incremental sync
  - `image_to_feature()` — OAM image → GeoJSON Feature
  - `generate_pmtiles()` — tippecanoe wrapper
- `requirements.txt` — boto3, requests
- `.github/workflows/sync.yml` — Daily sync workflow

## S3 Bucket Structure
```
oam-api/
├── state.json                 # {image_id: uploaded_at} sync state
├── all_images.geojson         # All image footprints (~20k features)
├── images.pmtiles             # Vector tiles (z0-12, layer "images")
└── meta/
    └── {image_id}             # Individual image JSON (no extension)
```

## OAM API Notes
- Base URL: `https://api.openaerialmap.org`
- `/meta` returns full metadata per image (footprint, bbox, TMS URLs) — no separate detail fetch needed
- ~20k images, ~200 pages at limit=100
- `sensor`, `tms`, `thumbnail` are nested under `properties` (not top-level)
- `geojson` field contains the footprint polygon geometry
- Pagination via `meta.found` / `meta.page` / `meta.limit`

## GeoJSON Feature Properties
`_id`, `title`, `provider`, `platform`, `sensor`, `gsd`, `file_size`, `acquisition_start`, `acquisition_end`, `tms`, `thumbnail`, `uploaded_at`

## Environment Variables
| Variable | Required | Description |
|----------|----------|-------------|
| `AWS_ACCESS_KEY_ID` | Yes | AWS credentials |
| `AWS_SECRET_ACCESS_KEY` | Yes | AWS credentials |
| `AWS_BUCKET_NAME` | Yes | Target S3 bucket |
| `AWS_REGION` | Yes | e.g. `us-east-1` |
| `S3_ENDPOINT_URL` | No | For S3-compatible storage (Source.coop) |

## Running Locally
```bash
pip install -r requirements.txt
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_BUCKET_NAME=... AWS_REGION=...
python etl.py
```
Requires `tippecanoe` installed for PMTiles generation.

## Key Design Decisions
- Skip rebuild entirely if no new/changed images (cost optimization)
- Individual image JSONs uploaded at `meta/{image_id}` (no .json extension, correct Content-Type)
- State file tracks `uploaded_at` timestamps — if an image's timestamp changes, it gets re-uploaded
- All images held in memory during ETL (feasible at ~20k scale)
