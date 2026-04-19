"""Sync Komoot planned-route details into the Notion Hike Planner database."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from kompy import KomootConnector
from notion_client import Client as NotionClient

METERS_TO_MILES = 0.000621371
METERS_TO_FEET = 3.28084

PLANNED_STATUSES = {"Planned", "Winter Planned"}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "Komoot-Notion-Sync/1.0 (https://github.com/nebriv/Komoot-Notion-Sync)"
NOMINATIM_MIN_INTERVAL = 1.5  # seconds; OSM policy is >=1s, leave headroom
OSRM_MIN_INTERVAL = 1.0
HTTP_TIMEOUT = 30

DEFAULT_ORIGIN_LAT = 40.7128
DEFAULT_ORIGIN_LON = -74.0060

_last_call: dict[str, float] = {}


def _throttled_get(host_key: str, url: str, interval: float) -> dict:
    last = _last_call.get(host_key, 0.0)
    wait = interval - (time.monotonic() - last)
    if wait > 0:
        time.sleep(wait)
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read()
    _last_call[host_key] = time.monotonic()
    return json.loads(body)


def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    qs = urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 14})
    data = _throttled_get("nominatim", f"{NOMINATIM_URL}?{qs}", NOMINATIM_MIN_INTERVAL)
    addr = data.get("address") or {}
    locality = (
        addr.get("hamlet")
        or addr.get("village")
        or addr.get("town")
        or addr.get("city")
        or addr.get("municipality")
        or addr.get("county")
    )
    region = addr.get("state") or addr.get("region")
    if locality and region:
        return f"{locality}, {region}"
    return data.get("display_name")


def driving_hours(
    origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float
) -> Optional[float]:
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    url = f"{OSRM_URL}/{coords}?overview=false"
    data = _throttled_get("osrm", url, OSRM_MIN_INTERVAL)
    routes = data.get("routes") or []
    if not routes:
        return None
    return round(routes[0]["duration"] / 3600, 2)


def parse_komoot_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (tour_id, share_token) from a Komoot tour URL."""
    match = re.search(r"/tour/(\d+)", url)
    if not match:
        return None, None
    tour_id = match.group(1)
    share_token = parse_qs(urlparse(url).query).get("share_token", [None])[0]
    return tour_id, share_token


def prop(page: dict, name: str) -> dict:
    return page["properties"].get(name, {})


def get_title(page: dict) -> str:
    for p in page["properties"].values():
        if p.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in p.get("title", []))
    return ""


def get_select(page: dict, name: str) -> Optional[str]:
    sel = prop(page, name).get("select")
    return sel["name"] if sel else None


def get_url(page: dict, name: str) -> Optional[str]:
    return prop(page, name).get("url")


def get_number(page: dict, name: str) -> Optional[float]:
    return prop(page, name).get("number")


def get_text(page: dict, name: str) -> Optional[str]:
    parts = prop(page, name).get("rich_text") or []
    text = "".join(t.get("plain_text", "") for t in parts).strip()
    return text or None


def resolve_data_source_id(notion: NotionClient, database_id: str) -> str:
    """Notion API 2025-09-03+ queries data sources, not databases directly."""
    db = notion.databases.retrieve(database_id=database_id)
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError(
            f"Database {database_id} has no data sources; "
            "set NOTION_DATA_SOURCE_ID explicitly."
        )
    return sources[0]["id"]


def iter_pages(notion: NotionClient, data_source_id: str) -> Iterator[dict]:
    cursor: Optional[str] = None
    while True:
        kwargs: dict = {"data_source_id": data_source_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.data_sources.query(**kwargs)
        for page in resp["results"]:
            yield page
        if not resp.get("has_more"):
            return
        cursor = resp.get("next_cursor")


def build_detail_update(
    tour,
    needs: dict,
    origin_lat: float,
    origin_lon: float,
) -> dict:
    props: dict = {}
    if needs["distance"] and getattr(tour, "distance", None):
        props["Planned Distance (miles)"] = {
            "number": round(tour.distance * METERS_TO_MILES, 2)
        }
    if needs["elevation"] and getattr(tour, "elevation_up", None):
        props["Planned Elevation Gain (feet)"] = {
            "number": round(tour.elevation_up * METERS_TO_FEET, 0)
        }

    start = getattr(tour, "start_point", None)
    if start is None or getattr(start, "lat", None) is None:
        return props

    if needs["start"]:
        try:
            place = reverse_geocode(start.lat, start.lon)
        except Exception as exc:
            print(f"       reverse-geocode failed: {exc}")
            place = None
        if place:
            props["Starting Point"] = {
                "rich_text": [{"type": "text", "text": {"content": place}}]
            }

    if needs["drive"]:
        try:
            hours = driving_hours(origin_lat, origin_lon, start.lat, start.lon)
        except Exception as exc:
            print(f"       drive-time lookup failed: {exc}")
            hours = None
        if hours is not None:
            props["Drive Time from NYC (hours)"] = {"number": hours}

    return props


def sync() -> int:
    load_dotenv()

    try:
        notion_token = os.environ["NOTION_TOKEN"]
        database_id = os.environ["NOTION_DATABASE_ID"]
        komoot_email = os.environ["KOMOOT_EMAIL"]
        komoot_password = os.environ["KOMOOT_PASSWORD"]
    except KeyError as missing:
        sys.stderr.write(f"Missing required env var: {missing}\n")
        return 1

    origin_lat = float(os.environ.get("DRIVE_ORIGIN_LAT", DEFAULT_ORIGIN_LAT))
    origin_lon = float(os.environ.get("DRIVE_ORIGIN_LON", DEFAULT_ORIGIN_LON))

    notion = NotionClient(auth=notion_token)
    komoot = KomootConnector(email=komoot_email, password=komoot_password)

    data_source_id = os.environ.get("NOTION_DATA_SOURCE_ID") or resolve_data_source_id(
        notion, database_id
    )

    updated = skipped = failed = 0

    for page in iter_pages(notion, data_source_id):
        name = get_title(page) or "(untitled)"
        status = get_select(page, "Hike Status")

        if status not in PLANNED_STATUSES:
            continue

        route_url = get_url(page, "Planned Route")
        needs = {
            "distance": get_number(page, "Planned Distance (miles)") is None,
            "elevation": get_number(page, "Planned Elevation Gain (feet)") is None,
            "start": get_text(page, "Starting Point") is None,
            "drive": get_number(page, "Drive Time from NYC (hours)") is None,
        }

        if not any(needs.values()):
            print(f"[skip] {name}: already has details")
            skipped += 1
            continue

        if not route_url:
            print(f"[skip] {name}: no Planned Route URL")
            skipped += 1
            continue

        tour_id, share_token = parse_komoot_url(route_url)
        if not tour_id:
            print(f"[skip] {name}: could not parse tour id from {route_url}")
            skipped += 1
            continue

        try:
            tour = komoot.get_tour_by_id(
                tour_identifier=tour_id,
                share_token=share_token,
            )
        except Exception as exc:
            print(f"[fail] {name}: komoot fetch failed for {tour_id}: {exc}")
            failed += 1
            continue

        props = build_detail_update(tour, needs, origin_lat, origin_lon)
        if not props:
            print(f"[skip] {name}: komoot tour had no usable data")
            skipped += 1
            continue

        try:
            notion.pages.update(page_id=page["id"], properties=props)
        except Exception as exc:
            print(f"[fail] {name}: notion update failed: {exc}")
            failed += 1
            continue

        filled = ", ".join(props.keys())
        print(f"[ok]   {name}: filled {filled}")
        updated += 1

    print(f"\nDone. updated={updated} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(sync())
