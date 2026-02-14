#!/usr/bin/env python3
"""
OpenAerialMap Cloud Native API Mirror ETL Script

Fetches image metadata from the OAM API, transforms it into cloud-native
formats, and uploads to S3 (or S3-compatible storage like Source.coop).

Unlike the HOT Tasking Manager mirror, OAM's /meta endpoint returns full
metadata per image (footprint, bbox, TMS URLs, etc.) in a single paginated
call — no separate detail fetch needed.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import requests
from botocore.config import Config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Constants
OAM_API_BASE = "https://api.openaerialmap.org"
META_ENDPOINT = f"{OAM_API_BASE}/meta"
STATE_FILE_KEY = "state.json"
ALL_IMAGES_GEOJSON = "all_images.geojson"
PMTILES_OUTPUT = "images.pmtiles"
PAGE_LIMIT = 100


class S3Client:
    """S3 client wrapper that supports custom endpoints for S3-compatible storage."""

    def __init__(self):
        self.bucket_name = os.environ["AWS_BUCKET_NAME"]
        self.region = os.environ.get("AWS_REGION", "us-east-1")
        endpoint_url = os.environ.get("S3_ENDPOINT_URL")

        client_kwargs = {
            "service_name": "s3",
            "region_name": self.region,
            "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
            "config": Config(signature_version="s3v4"),
        }

        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            logger.info(f"Using custom S3 endpoint: {endpoint_url}")
        else:
            logger.info("Using standard AWS S3")

        self.client = boto3.client(**client_kwargs)

    def get_object(self, key: str) -> bytes | None:
        """Get an object from S3, returns None if not found."""
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key)
            return response["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None
        except Exception as e:
            logger.warning(f"Error fetching {key}: {e}")
            return None

    def put_object(self, key: str, body: bytes, content_type: str) -> None:
        """Upload an object to S3 with specified content type."""
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
        logger.debug(f"Uploaded: {key} ({content_type})")

    def list_objects(self, prefix: str) -> list[str]:
        """List all object keys with a given prefix."""
        keys = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys


class OAMApiClient:
    """Client for the OpenAerialMap API."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "OAM-CloudNativeMirror/1.0",
            }
        )

    def get_images_page(self, page: int = 1) -> dict[str, Any]:
        """Fetch a page of image metadata from the API."""
        params = {"page": page, "limit": PAGE_LIMIT}
        response = self.session.get(META_ENDPOINT, params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def get_all_images(self) -> list[dict[str, Any]]:
        """Fetch all images by paginating through /meta."""
        all_images = []
        page = 1

        # Fetch first page to get total count
        logger.info("Fetching images page 1...")
        data = self.get_images_page(page)
        results = data.get("results", [])
        all_images.extend(results)

        meta = data.get("meta", {})
        total_found = meta.get("found", 0)
        total_pages = (total_found + PAGE_LIMIT - 1) // PAGE_LIMIT

        logger.info(f"Found {total_found} images across {total_pages} pages")

        # Fetch remaining pages
        for page in range(2, total_pages + 1):
            logger.info(f"Fetching images page {page}/{total_pages}...")
            try:
                data = self.get_images_page(page)
                results = data.get("results", [])
                if not results:
                    break
                all_images.extend(results)
            except requests.RequestException as e:
                logger.error(f"Failed to fetch page {page}: {e}")
                continue

        logger.info(f"Fetched {len(all_images)} images total")
        return all_images


class StateManager:
    """Manages incremental sync state."""

    def __init__(self, s3_client: S3Client):
        self.s3_client = s3_client
        self.state: dict[str, str] = {}  # image_id -> uploaded_at timestamp

    def load(self) -> None:
        """Load state from S3."""
        data = self.s3_client.get_object(STATE_FILE_KEY)
        if data:
            self.state = json.loads(data.decode("utf-8"))
            logger.info(f"Loaded state with {len(self.state)} images")
        else:
            logger.info("No existing state found, starting fresh")
            self.state = {}

    def save(self) -> None:
        """Save state to S3."""
        self.s3_client.put_object(
            STATE_FILE_KEY,
            json.dumps(self.state, indent=2).encode("utf-8"),
            "application/json",
        )
        logger.info(f"Saved state with {len(self.state)} images")

    def needs_update(self, image_id: str, uploaded_at: str) -> bool:
        """Check if an image needs to be updated based on uploaded_at timestamp."""
        stored_timestamp = self.state.get(image_id)
        if not stored_timestamp:
            return True
        return uploaded_at != stored_timestamp

    def mark_updated(self, image_id: str, uploaded_at: str) -> None:
        """Mark an image as updated in state."""
        self.state[image_id] = uploaded_at


def image_to_feature(image: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an OAM image object to a GeoJSON Feature."""
    geojson = image.get("geojson")
    if not geojson:
        return None

    img_props = image.get("properties", {})

    properties = {
        "_id": image.get("_id"),
        "title": image.get("title"),
        "provider": image.get("provider"),
        "platform": image.get("platform"),
        "sensor": img_props.get("sensor"),
        "gsd": image.get("gsd"),
        "file_size": image.get("file_size"),
        "acquisition_start": image.get("acquisition_start"),
        "acquisition_end": image.get("acquisition_end"),
        "tms": img_props.get("tms"),
        "thumbnail": img_props.get("thumbnail"),
        "uploaded_at": image.get("uploaded_at"),
    }

    return {
        "type": "Feature",
        "geometry": geojson,
        "properties": properties,
    }


def generate_pmtiles(geojson_path: Path, output_path: Path) -> bool:
    """Generate PMTiles from GeoJSON using tippecanoe."""
    logger.info("Generating PMTiles with tippecanoe...")

    cmd = [
        "tippecanoe",
        "-o",
        str(output_path),
        "-z",
        "12",  # Max zoom
        "-Z",
        "0",  # Min zoom
        "--force",  # Overwrite existing
        "--no-feature-limit",
        "--no-tile-size-limit",
        "-l",
        "images",  # Layer name
        str(geojson_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info("PMTiles generation complete")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"tippecanoe failed: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error("tippecanoe not found. Please install it first.")
        return False


def run_etl():
    """Main ETL process."""
    logger.info("Starting OpenAerialMap Cloud Native Mirror ETL")

    # Initialize clients
    s3_client = S3Client()
    api_client = OAMApiClient()
    state_manager = StateManager(s3_client)

    # Load existing state
    state_manager.load()

    # Fetch all images via /meta (full metadata in one pass)
    all_images = api_client.get_all_images()

    # Identify new/changed images
    images_to_update = []
    for image in all_images:
        image_id = image.get("_id")
        uploaded_at = image.get("uploaded_at")

        if image_id and uploaded_at:
            if state_manager.needs_update(image_id, uploaded_at):
                images_to_update.append(image)

    logger.info(f"{len(images_to_update)} images need updating")

    # If no images need updating, skip everything
    if not images_to_update:
        logger.info("No changes detected, skipping uploads")
        logger.info("ETL complete!")
        return

    # Upload new/changed image JSONs
    for image in images_to_update:
        image_id = image["_id"]
        uploaded_at = image["uploaded_at"]

        s3_key = f"meta/{image_id}"
        s3_client.put_object(
            s3_key,
            json.dumps(image, indent=2).encode("utf-8"),
            "application/json",
        )

        state_manager.mark_updated(image_id, uploaded_at)

    logger.info(f"Uploaded {len(images_to_update)} image metadata files")

    # Build master GeoJSON from ALL images (already in memory)
    logger.info("Building master GeoJSON FeatureCollection...")

    features = []
    for image in all_images:
        feature = image_to_feature(image)
        if feature:
            features.append(feature)

    feature_collection = {"type": "FeatureCollection", "features": features}
    logger.info(f"Created FeatureCollection with {len(features)} features")

    # Use temp directory for intermediate files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        geojson_path = tmpdir_path / ALL_IMAGES_GEOJSON
        pmtiles_path = tmpdir_path / PMTILES_OUTPUT

        # Write GeoJSON to disk (for tippecanoe)
        with open(geojson_path, "w") as f:
            json.dump(feature_collection, f)

        # Upload GeoJSON to S3
        s3_client.put_object(
            ALL_IMAGES_GEOJSON,
            json.dumps(feature_collection).encode("utf-8"),
            "application/geo+json",
        )
        logger.info(f"Uploaded {ALL_IMAGES_GEOJSON}")

        # Generate and upload PMTiles
        if generate_pmtiles(geojson_path, pmtiles_path):
            with open(pmtiles_path, "rb") as f:
                pmtiles_data = f.read()

            s3_client.put_object(
                PMTILES_OUTPUT,
                pmtiles_data,
                "application/vnd.pmtiles",
            )
            logger.info(f"Uploaded {PMTILES_OUTPUT}")
        else:
            logger.warning("PMTiles generation failed, skipping upload")

    # Save updated state
    state_manager.save()

    logger.info("ETL complete!")


def validate_env():
    """Validate required environment variables are set."""
    required = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_BUCKET_NAME"]
    missing = [var for var in required if not os.environ.get(var)]

    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)


if __name__ == "__main__":
    validate_env()
    run_etl()
