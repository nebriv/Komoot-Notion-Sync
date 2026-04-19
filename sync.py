"""Sync Komoot planned-route details into the Notion Hike Planner database."""

from __future__ import annotations

import os
import re
import sys
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from kompy import KomootConnector
from notion_client import Client as NotionClient

METERS_TO_MILES = 0.000621371
METERS_TO_FEET = 3.28084

PLANNED_STATUSES = {"Planned", "Winter Planned"}


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


def iter_pages(notion: NotionClient, database_id: str) -> Iterator[dict]:
    cursor: Optional[str] = None
    while True:
        kwargs: dict = {"database_id": database_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for page in resp["results"]:
            yield page
        if not resp.get("has_more"):
            return
        cursor = resp.get("next_cursor")


def build_detail_update(tour, have_distance: bool, have_elevation: bool) -> dict:
    props: dict = {}
    if not have_distance and getattr(tour, "distance", None):
        props["Planned Distance (miles)"] = {
            "number": round(tour.distance * METERS_TO_MILES, 2)
        }
    if not have_elevation and getattr(tour, "elevation_up", None):
        props["Planned Elevation Gain (feet)"] = {
            "number": round(tour.elevation_up * METERS_TO_FEET, 0)
        }
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

    notion = NotionClient(auth=notion_token)
    komoot = KomootConnector(email=komoot_email, password=komoot_password)

    updated = skipped = failed = 0

    for page in iter_pages(notion, database_id):
        name = get_title(page) or "(untitled)"
        status = get_select(page, "Hike Status")

        if status not in PLANNED_STATUSES:
            continue

        route_url = get_url(page, "Planned Route")
        distance = get_number(page, "Planned Distance (miles)")
        elevation = get_number(page, "Planned Elevation Gain (feet)")

        if distance is not None and elevation is not None:
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

        props = build_detail_update(
            tour,
            have_distance=distance is not None,
            have_elevation=elevation is not None,
        )
        if not props:
            print(f"[skip] {name}: komoot tour had no distance/elevation data")
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
