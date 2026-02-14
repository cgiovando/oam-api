#!/usr/bin/env python3
"""
OpenAerialMap Quarterly Stats Generator

Connects to the OAM production MongoDB to compute quarterly reporting metrics:
- # contributors (unique uploaders)
- # images uploaded
- # UAV images
- # sq km covered (geodesic area from footprints)
- Cumulative versions of each

Outputs JSON + CSV to S3.
"""

import csv
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from pymongo import MongoClient
from pyproj import Geod
from shapely.geometry import shape

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

GEOD = Geod(ellps="WGS84")


def connect_mongo():
    """Connect to OAM production MongoDB."""
    uri = os.environ["MONGODB_URI"]
    client = MongoClient(uri, serverSelectionTimeoutMS=30000)
    db_name = os.environ.get("MONGODB_DATABASE", "oam-api-production")
    db = client[db_name]
    # Test connection
    db.list_collection_names()
    logger.info(f"Connected to MongoDB: {db_name}")
    return db


def get_quarterly_contributors(db):
    """Count unique contributors per quarter from uploads collection."""
    pipeline = [
        {"$project": {
            "year": {"$year": "$createdAt"},
            "quarter": {"$ceil": {"$divide": [{"$month": "$createdAt"}, 3]}},
            "user": 1,
        }},
        {"$group": {
            "_id": {"year": "$year", "quarter": "$quarter"},
            "unique_users": {"$addToSet": "$user"},
        }},
        {"$project": {
            "contributors": {"$size": "$unique_users"},
        }},
        {"$sort": {"_id.year": 1, "_id.quarter": 1}},
    ]
    results = {}
    for doc in db.uploads.aggregate(pipeline):
        key = (doc["_id"]["year"], int(doc["_id"]["quarter"]))
        results[key] = doc["contributors"]
    logger.info(f"Got contributor counts for {len(results)} quarters")
    return results


def get_quarterly_images(db):
    """Count images and UAV images per quarter from metas collection."""
    pipeline = [
        {"$match": {"uploaded_at": {"$exists": True, "$ne": None}}},
        {"$project": {
            "year": {"$year": "$uploaded_at"},
            "quarter": {"$ceil": {"$divide": [{"$month": "$uploaded_at"}, 3]}},
            "is_uav": {"$in": [{"$toLower": "$platform"}, ["uav"]]},
        }},
        {"$group": {
            "_id": {"year": "$year", "quarter": "$quarter"},
            "images": {"$sum": 1},
            "uav_images": {"$sum": {"$cond": ["$is_uav", 1, 0]}},
        }},
        {"$sort": {"_id.year": 1, "_id.quarter": 1}},
    ]
    results = {}
    for doc in db.metas.aggregate(pipeline):
        key = (doc["_id"]["year"], int(doc["_id"]["quarter"]))
        results[key] = {
            "images": doc["images"],
            "uav_images": doc["uav_images"],
        }
    logger.info(f"Got image counts for {len(results)} quarters")
    return results


def compute_quarterly_area(db):
    """Compute total area (sq km) per quarter from metas footprints."""
    logger.info("Computing areas from footprints (this may take a minute)...")

    cursor = db.metas.find(
        {"geojson": {"$exists": True}, "uploaded_at": {"$exists": True, "$ne": None}},
        {"geojson": 1, "uploaded_at": 1},
    )

    results = {}
    processed = 0
    errors = 0

    for doc in cursor:
        try:
            uploaded_at = doc["uploaded_at"]
            year = uploaded_at.year
            quarter = (uploaded_at.month - 1) // 3 + 1
            key = (year, quarter)

            geom = shape(doc["geojson"])
            area_m2 = abs(GEOD.geometry_area_perimeter(geom)[0])
            area_km2 = area_m2 / 1e6

            results[key] = results.get(key, 0.0) + area_km2
            processed += 1
        except Exception:
            errors += 1

    logger.info(f"Computed area for {processed} images ({errors} errors)")
    return results


def build_quarterly_table(contributors, images, areas):
    """Combine all metrics into a sorted quarterly table with cumulative totals."""
    all_keys = sorted(set(contributors) | set(images) | set(areas))

    rows = []
    cum_contributors = set()
    cum_images = 0
    cum_uav = 0
    cum_area = 0.0

    # For cumulative unique contributors, we need to re-query per quarter
    # Instead, we'll sum quarterly new contributors as an approximation
    # and also track cumulative images/uav/area precisely
    cum_contrib_count = 0

    for key in all_keys:
        year, quarter = key
        q_contributors = contributors.get(key, 0)
        q_images_data = images.get(key, {"images": 0, "uav_images": 0})
        q_images = q_images_data["images"]
        q_uav = q_images_data["uav_images"]
        q_area = areas.get(key, 0.0)

        cum_contrib_count += q_contributors
        cum_images += q_images
        cum_uav += q_uav
        cum_area += q_area

        rows.append({
            "year": year,
            "quarter": quarter,
            "period": f"{year} Q{quarter}",
            "contributors": q_contributors,
            "images": q_images,
            "uav_images": q_uav,
            "area_sq_km": round(q_area, 2),
            "cumulative_contributors": cum_contrib_count,
            "cumulative_images": cum_images,
            "cumulative_uav_images": cum_uav,
            "cumulative_area_sq_km": round(cum_area, 2),
        })

    return rows


def get_cumulative_contributors(db):
    """Compute true cumulative unique contributors over time."""
    pipeline = [
        {"$project": {
            "year": {"$year": "$createdAt"},
            "quarter": {"$ceil": {"$divide": [{"$month": "$createdAt"}, 3]}},
            "user": 1,
        }},
        {"$sort": {"_id": 1}},
    ]

    # Collect all (quarter, user) pairs
    quarter_users = {}
    for doc in db.uploads.aggregate(pipeline, allowDiskUse=True):
        key = (doc["year"], int(doc["quarter"]))
        if key not in quarter_users:
            quarter_users[key] = set()
        quarter_users[key].add(str(doc.get("user", "")))

    # Compute cumulative unique users
    all_users = set()
    cumulative = {}
    for key in sorted(quarter_users):
        all_users |= quarter_users[key]
        cumulative[key] = len(all_users)

    return cumulative


def to_csv(rows):
    """Convert rows to CSV string."""
    output = io.StringIO()
    fieldnames = [
        "period", "contributors", "images", "uav_images", "area_sq_km",
        "cumulative_contributors", "cumulative_images",
        "cumulative_uav_images", "cumulative_area_sq_km",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def upload_to_s3(data_bytes, key, content_type):
    """Upload stats to S3."""
    bucket = os.environ.get("AWS_BUCKET_NAME")
    if not bucket:
        logger.warning("AWS_BUCKET_NAME not set, skipping S3 upload")
        return

    region = os.environ.get("AWS_REGION", "us-east-1")
    client = boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4"),
    )
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=data_bytes,
        ContentType=content_type,
    )
    logger.info(f"Uploaded {key} to s3://{bucket}")


def run_stats():
    """Main stats pipeline."""
    logger.info("Starting OAM quarterly stats generation")

    db = connect_mongo()

    # Gather all metrics
    contributors = get_quarterly_contributors(db)
    image_data = get_quarterly_images(db)
    areas = compute_quarterly_area(db)

    # Build table
    rows = build_quarterly_table(contributors, image_data, areas)

    # Fix cumulative contributors with true unique count
    cumulative = get_cumulative_contributors(db)
    for row in rows:
        key = (row["year"], row["quarter"])
        if key in cumulative:
            row["cumulative_contributors"] = cumulative[key]

    # Output JSON
    stats_output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_images": sum(r["images"] for r in rows),
        "total_uav_images": sum(r["uav_images"] for r in rows),
        "total_area_sq_km": round(sum(r["area_sq_km"] for r in rows), 2),
        "total_contributors": cumulative[max(cumulative)] if cumulative else 0,
        "quarters": rows,
    }

    json_bytes = json.dumps(stats_output, indent=2).encode("utf-8")
    csv_str = to_csv(rows)
    csv_bytes = csv_str.encode("utf-8")

    # Print summary
    print("\n=== OAM Quarterly Stats ===\n")
    print(f"{'Period':<12} {'Contrib':>8} {'Images':>8} {'UAV':>8} {'Area km²':>12}  "
          f"{'Cum Contrib':>12} {'Cum Images':>12} {'Cum UAV':>10} {'Cum km²':>12}")
    print("-" * 110)
    for r in rows:
        print(f"{r['period']:<12} {r['contributors']:>8} {r['images']:>8} {r['uav_images']:>8} "
              f"{r['area_sq_km']:>12,.2f}  {r['cumulative_contributors']:>12,} "
              f"{r['cumulative_images']:>12,} {r['cumulative_uav_images']:>10,} "
              f"{r['cumulative_area_sq_km']:>12,.2f}")

    print(f"\nTotal: {stats_output['total_images']:,} images, "
          f"{stats_output['total_uav_images']:,} UAV, "
          f"{stats_output['total_area_sq_km']:,.2f} sq km, "
          f"{stats_output['total_contributors']:,} contributors")

    # Upload to S3
    upload_to_s3(json_bytes, "stats.json", "application/json")
    upload_to_s3(csv_bytes, "stats.csv", "text/csv")

    # Also write locally for inspection
    with open("stats.json", "w") as f:
        f.write(json_bytes.decode("utf-8"))
    with open("stats.csv", "w") as f:
        f.write(csv_str)

    logger.info("Stats generation complete!")


if __name__ == "__main__":
    run_stats()
