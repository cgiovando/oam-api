# OAM Cloud-Native API Mirror

Static mirror of the [OpenAerialMap](https://openaerialmap.org) catalog. A daily ETL snapshots the entire OAM `/meta` endpoint into static files on S3 — no runtime server, no database queries at read time.

## Data Access

| Resource | URL |
|----------|-----|
| All images GeoJSON | `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/all_images.geojson` |
| Vector tiles (PMTiles) | `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/images.pmtiles` |
| Single image metadata | `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/meta/{image_id}` |

| Quarterly stats (JSON) | `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/stats.json` |
| Quarterly stats (CSV) | `https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/stats.csv` |

All endpoints support CORS and are publicly accessible.

## How It Works

```
OAM API (/meta) → GitHub Actions (daily cron) → etl.py → S3
                                                    │
                                               tippecanoe
                                                    │
                                               PMTiles
```

1. **Paginate** through all ~20k images via the OAM `/meta` endpoint
2. **Compare** `uploaded_at` timestamps against saved state — skip unchanged images
3. **Upload** new/changed image metadata as individual JSON files to S3
4. **Build** a master GeoJSON FeatureCollection of all image footprints
5. **Generate** PMTiles vector tiles via [tippecanoe](https://github.com/felt/tippecanoe)
6. **Upload** GeoJSON + PMTiles to S3

If nothing changed since the last run, the ETL exits early (no S3 writes, no cost).

## GeoJSON Properties

Each feature in `all_images.geojson` includes:

| Property | Description |
|----------|-------------|
| `_id` | OAM image ID |
| `title` | Image title |
| `provider` | Data provider |
| `platform` | Capture platform (uav, satellite, etc.) |
| `sensor` | Sensor name |
| `gsd` | Ground sample distance (resolution) |
| `file_size` | File size in bytes |
| `acquisition_start` | Acquisition start date |
| `acquisition_end` | Acquisition end date |
| `tms` | TMS tile URL template |
| `thumbnail` | Thumbnail image URL |
| `uploaded_at` | Upload timestamp |

## Using the PMTiles

Load `images.pmtiles` directly in MapLibre GL JS:

```js
import { Protocol } from "pmtiles";

let protocol = new Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

map.addSource("oam-images", {
  type: "vector",
  url: "pmtiles://https://cgiovando-oam-api.s3.us-east-1.amazonaws.com/images.pmtiles",
});

map.addLayer({
  id: "oam-images-fill",
  type: "fill",
  source: "oam-images",
  "source-layer": "images",
  paint: {
    "fill-color": "#d73f3f",
    "fill-opacity": 0.3,
  },
});
```

## Running Locally

```bash
pip install -r requirements.txt
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_BUCKET_NAME=cgiovando-oam-api
export AWS_REGION=us-east-1
python etl.py
```

Requires [tippecanoe](https://github.com/felt/tippecanoe) for PMTiles generation.

### Quarterly Stats

```bash
pip install -r requirements.txt
export MONGODB_URI=mongodb+srv://user:pass@host/oam-api-production
export AWS_BUCKET_NAME=cgiovando-oam-api
export AWS_REGION=us-east-1
python stats.py
```

Connects to the OAM production MongoDB to compute quarterly metrics:
contributors, images uploaded, UAV images, and area coverage (sq km).
Outputs `stats.json` and `stats.csv` to S3.

## Related

- [hot-tm-cn-api](https://github.com/cgiovando/hot-tm-cn-api) — Same pattern for the HOT Tasking Manager
- [OpenAerialMap](https://openaerialmap.org) — The upstream imagery catalog
