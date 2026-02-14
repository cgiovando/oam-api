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
- **Stats**: pymongo, shapely, pyproj (geodesic area from footprints)
- **Vector tiles**: tippecanoe → PMTiles
- **Storage**: AWS S3 (or S3-compatible via `S3_ENDPOINT_URL`)
- **Database**: MongoDB Atlas (oam-api-production cluster, read-only access)
- **CI/CD**: GitHub Actions (daily ETL cron + monthly stats cron)

## Key Files
- `etl.py` — Main ETL script (~270 lines)
  - `S3Client` — S3/S3-compatible upload/download
  - `OAMApiClient` — Paginates `/meta?page={n}&limit=100`
  - `StateManager` — Tracks `uploaded_at` per image for incremental sync
  - `image_to_feature()` — OAM image → GeoJSON Feature
  - `generate_pmtiles()` — tippecanoe wrapper
- `stats.py` — Quarterly stats generator (~250 lines)
  - Connects to OAM production MongoDB
  - Computes: contributors, images, UAV images, area (sq km) per quarter
  - True cumulative unique contributors (not just summed quarterly counts)
  - Outputs JSON + CSV to S3
- `dashboard.html` — Static stats dashboard (single HTML file, Chart.js from CDN)
  - Fetches `stats.json` from S3, renders 4 summary cards + 4 charts
  - Deployed to S3 as `index.html`
  - Auto-updates when stats.py refreshes the data (no redeployment needed)
- `requirements.txt` — boto3, requests, pymongo, shapely, pyproj
- `.github/workflows/sync.yml` — Daily ETL sync workflow (midnight UTC)
- `.github/workflows/stats.yml` — Monthly stats workflow (1st of month, 06:00 UTC)

## S3 Bucket Structure
```
cgiovando-oam-api/
├── index.html                 # Stats dashboard (from dashboard.html)
├── state.json                 # {image_id: uploaded_at} sync state
├── all_images.geojson         # All image footprints (~20k features)
├── images.pmtiles             # Vector tiles (z0-12, layer "images")
├── stats.json                 # Quarterly stats (JSON)
├── stats.csv                  # Quarterly stats (CSV)
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
| `MONGODB_URI` | stats.py | MongoDB connection string |

## MongoDB Notes
- Cluster: `oam-api-production.6ioyq.mongodb.net`
- Database: `oam-api-production`
- Key collections: `metas` (20k), `uploads` (17k), `users` (51k), `images` (42k), `analytics` (381k)
- `metas` has footprints (`geojson`), `platform`, `uploaded_at`, `gsd`, `file_size`
- `uploads` links users to scenes via `user` (ObjectId) and `createdAt`
- Platform values are mixed case: "uav", "UAV", "satellite", "Satellite", "aircraft", "balloon", "kite"
- `uploads` collection starts from Q4 2017 (no contributor tracking before that)

## Running Locally
```bash
# ETL (daily sync from OAM API → S3)
pip install -r requirements.txt
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_BUCKET_NAME=cgiovando-oam-api AWS_REGION=us-east-1
python etl.py    # requires tippecanoe installed

# Stats (quarterly metrics from MongoDB → S3)
export MONGODB_URI=mongodb+srv://cri-db-reader:<password>@oam-api-production.6ioyq.mongodb.net/oam-api-production
python stats.py

# Dashboard (re-upload after editing)
aws s3 cp dashboard.html s3://cgiovando-oam-api/index.html --content-type "text/html"
```

## Live URLs
- Dashboard: `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/index.html`
- GeoJSON: `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/all_images.geojson`
- PMTiles: `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/images.pmtiles`
- Stats JSON: `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/stats.json`
- Stats CSV: `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/stats.csv`
- Single image: `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/meta/{image_id}`

## GitHub
- Repo: `cgiovando/oam-api`
- Secrets: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_BUCKET_NAME`, `AWS_REGION`, `MONGODB_URI`
- IAM user: `oam-api-etl` (scoped to S3 bucket only)

## Key Design Decisions
- Skip rebuild entirely if no new/changed images (cost optimization)
- Individual image JSONs uploaded at `meta/{image_id}` (no .json extension, correct Content-Type)
- State file tracks `uploaded_at` timestamps — if an image's timestamp changes, it gets re-uploaded
- All images held in memory during ETL (feasible at ~20k scale)
- Dashboard is a single HTML file with Chart.js CDN — no build step, auto-updates from stats.json
- dashboard.html must be manually re-uploaded to S3 as index.html after edits (not in any workflow)
