#!/usr/bin/env python3
"""Fetch a Notion database and export all dated entries as an .ics calendar file."""

import os
import re
import sys
from datetime import date, datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from icalendar import Calendar, Event, Timezone, TimezoneStandard, vGeo, vText

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "notion_calendar.ics")

NOTION_API_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")


def query_database(database_id: str) -> list[dict]:
    """Query all pages from a Notion database, handling pagination."""
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_API_VERSION,
        "Content-Type": "application/json",
    }

    results = []
    payload: dict = {}

    while True:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return results


def find_date_property(properties: dict) -> tuple[str, dict] | None:
    """Find the first date-type property in a page's properties."""
    for name, prop in properties.items():
        if prop["type"] == "date" and prop.get("date"):
            return name, prop["date"]
    return None


def get_title(properties: dict) -> str:
    """Extract the title from a page's properties."""
    for prop in properties.values():
        if prop["type"] == "title":
            parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in parts)
    return "Untitled"


def get_rich_text(properties: dict, name: str) -> str:
    """Extract plain text from a rich_text property by name."""
    prop = properties.get(name)
    if not prop or prop["type"] != "rich_text":
        return ""
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


def get_url_property(properties: dict, name: str) -> str:
    """Extract a URL property value by name."""
    prop = properties.get(name)
    if not prop or prop["type"] != "url":
        return ""
    return prop.get("url") or ""


def get_select(properties: dict, name: str) -> str:
    """Extract a select or status property value by name."""
    prop = properties.get(name)
    if not prop or prop["type"] not in ("select", "status"):
        return ""
    val = prop.get(prop["type"])
    return val.get("name", "") if val else ""


def get_multi_select(properties: dict, name: str) -> list[str]:
    """Extract a multi_select property as a list of strings."""
    prop = properties.get(name)
    if not prop or prop["type"] != "multi_select":
        return []
    return [v.get("name", "") for v in prop.get("multi_select", []) if v.get("name")]


def build_description(properties: dict) -> str:
    """
    Compose a human-readable description from the event's metadata fields.
    Pulls from rich-text notes first, then appends structured fields.
    """
    lines: list[str] = []

    # Free-text notes
    for candidate in ("Description", "Notes", "Notizen", "Beschreibung", "Text"):
        text = get_rich_text(properties, candidate)
        if text:
            lines.append(text)
            break

    # Structured metadata
    meta: list[str] = []

    status = get_select(properties, "Status")
    if status:
        meta.append(f"Status: {status}")

    types = get_multi_select(properties, "Type")
    if types:
        meta.append(f"Type: {', '.join(types)}")

    city = get_select(properties, "City")
    if city:
        meta.append(f"City: {city}")

    day = get_select(properties, "Travel Day")
    if day:
        meta.append(f"Travel Day: {day}")

    if meta:
        if lines:
            lines.append("")  # blank separator
        lines.extend(meta)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

# Patterns that indicate a Google Maps URL
_GMAPS_PATTERNS = [
    # maps.google.com/?q=lat,lng  or  ?ll=lat,lng
    re.compile(r"[?&](?:q|ll)=(-?\d+\.?\d*),(-?\d+\.?\d*)"),
    # google.com/maps/place/.../@ lat,lng,zoom
    re.compile(r"@(-?\d+\.?\d*),(-?\d+\.?\d*)"),
    # maps.app.goo.gl short links — resolved below if needed
]

_GMAPS_SHORT_RE = re.compile(r"maps\.app\.goo\.gl|goo\.gl/maps")


def _extract_coords_from_url(url: str) -> tuple[float, float] | None:
    """Try to parse lat/lng from a Google Maps URL. Returns (lat, lng) or None."""
    for pattern in _GMAPS_PATTERNS:
        m = pattern.search(url)
        if m:
            try:
                return float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
    return None


def _resolve_short_url(url: str) -> str:
    """Follow a redirect once to expand a short Maps URL."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=5)
        return resp.url
    except Exception:
        return url


def find_location(properties: dict) -> tuple[str, tuple[float, float] | None]:
    """
    Return (location_string, (lat, lng) | None) from a Notion page's properties.

    Searches in order:
    1. Native Notion 'place' property type  ← lat/lon/name/address built-in
    2. Rich-text properties with common location names
    3. URL properties — if it looks like Google Maps, parse or resolve coords
    """
    # 1. Native place property (Notion's built-in map type)
    for name, prop in properties.items():
        if prop.get("type") == "place" and prop.get("place"):
            place = prop["place"]
            lat = place.get("lat")
            lon = place.get("lon")
            label = place.get("name") or place.get("address") or name
            coords = (lat, lon) if lat is not None and lon is not None else None
            return label, coords

    location_candidates = (
        "Location",
        "Place",
        "Lokasi",
        "Tempat",
        "Address",
        "Alamat",
        "Map",
    )

    # 2. Rich-text location field
    for name in location_candidates:
        text = get_rich_text(properties, name)
        if text:
            coords = _extract_coords_from_url(text)
            return text, coords

    # 3. URL properties — check for Maps links first
    url_props: list[tuple[str, str]] = []
    for name, prop in properties.items():
        if prop["type"] == "url" and prop.get("url"):
            url_props.append((name, prop["url"]))

    for name, url in url_props:
        parsed = urlparse(url)
        if "google.com/maps" in parsed.netloc + parsed.path or _GMAPS_SHORT_RE.search(
            url
        ):
            expanded = _resolve_short_url(url) if _GMAPS_SHORT_RE.search(url) else url
            coords = _extract_coords_from_url(expanded)
            label = name if name.lower() not in ("url", "link") else "Google Maps"
            return label, coords

    for name, url in url_props:
        if (
            name.lower() in location_candidates
            or "map" in name.lower()
            or "loc" in name.lower()
        ):
            return url, None

    return "", None


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def parse_datetime(value: str, time_zone: str | None = None) -> datetime | date:
    """Parse a Notion date string into a datetime or date object."""
    tz = None
    if time_zone:
        try:
            tz = ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, KeyError):
            pass

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            if tz is not None:
                dt = dt.astimezone(tz)
            return dt
        except ValueError:
            continue

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(value, fmt)
            return naive.replace(tzinfo=tz if tz is not None else timezone.utc)
        except ValueError:
            continue

    return date.fromisoformat(value)


def is_in_past(start, end) -> bool:
    reference = end if end is not None else start
    today = date.today()
    now = datetime.now(timezone.utc)

    if isinstance(reference, datetime):
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        return reference < now
    return reference < today


def ensure_vtimezone(cal: Calendar, tz: ZoneInfo | timezone) -> None:
    """Add a VTIMEZONE block for the given timezone if not already present."""
    if isinstance(tz, timezone):
        tzid = "UTC"
        if any(
            c.name == "VTIMEZONE" and str(c.get("TZID", "")) == tzid
            for c in cal.subcomponents
        ):
            return
        tzc = Timezone()
        tzc.add("tzid", tzid)
        tzs = TimezoneStandard()
        tzs.add("dtstart", datetime(1970, 1, 1, tzinfo=timezone.utc))
        tzs.add("tzoffsetfrom", timezone.utc.utcoffset(None))
        tzs.add("tzoffsetto", timezone.utc.utcoffset(None))
        tzs.add("tzname", "UTC")
        tzc.add_component(tzs)
        cal.add_component(tzc)
        return

    tzid = str(tz)
    if any(
        c.name == "VTIMEZONE" and str(c.get("TZID", "")) == tzid
        for c in cal.subcomponents
    ):
        return

    now_utc = datetime(2024, 1, 1, tzinfo=timezone.utc)
    offset = tz.utcoffset(now_utc)

    tzc = Timezone()
    tzc.add("tzid", tzid)
    tzs = TimezoneStandard()
    tzs.add("dtstart", datetime(1970, 1, 1, tzinfo=timezone.utc))
    tzs.add("tzoffsetfrom", offset)
    tzs.add("tzoffsetto", offset)
    tzs.add("tzname", tzid)
    tzc.add_component(tzs)
    cal.add_component(tzc)


def build_calendar(pages: list[dict]) -> Calendar:
    """Build an iCalendar object from Notion pages."""
    cal = Calendar()
    cal.add("prodid", "-//notion2cal//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", "Notion Calendar")

    ensure_vtimezone(cal, timezone.utc)

    skipped = 0
    skipped_past = 0

    for page in pages:
        props = page.get("properties", {})
        date_info = find_date_property(props)
        if not date_info:
            skipped += 1
            continue

        _, date_data = date_info
        start_raw = date_data.get("start")
        end_raw = date_data.get("end")
        time_zone = date_data.get("time_zone")

        if not start_raw:
            skipped += 1
            continue

        title = get_title(props)
        description = build_description(props)

        if DEBUG:
            print(f"\n[{title}] properties:")
            for pname, pval in props.items():
                ptype = pval.get("type", "?")
                # Show the raw value for easy inspection
                raw = pval.get(ptype)
                print(f"  {pname!r:30s} ({ptype}) = {str(raw)[:120]}")

        location_str, coords = find_location(props)

        if DEBUG:
            print(f"  → location={location_str!r}  coords={coords}")
        start = parse_datetime(start_raw, time_zone)
        end = parse_datetime(end_raw, time_zone) if end_raw else None

        if is_in_past(start, end):
            skipped_past += 1
            continue

        if isinstance(start, datetime) and start.tzinfo is not None:
            ensure_vtimezone(cal, start.tzinfo)

        event = Event()
        event.add("summary", title)
        event.add("dtstart", start)

        if end:
            event.add("dtend", end)
        elif isinstance(start, date) and not isinstance(start, datetime):
            pass
        else:
            event.add("dtend", start)

        if description:
            event.add("description", description)

        # LOCATION — plain address string shown in calendar apps
        if location_str:
            event.add("location", location_str)

        # GEO — machine-readable lat/lng (shows map pin in Apple Calendar etc.)
        if coords:
            lat, lng = coords
            event.add("geo", (lat, lng))
            # X-APPLE-STRUCTURED-LOCATION gives Apple Calendar a richer map card
            event["X-APPLE-STRUCTURED-LOCATION"] = vText(f"geo:{lat},{lng}")
            event["X-APPLE-STRUCTURED-LOCATION"].params["VALUE"] = "URI"
            event["X-APPLE-STRUCTURED-LOCATION"].params["X-ADDRESS"] = location_str
            event["X-APPLE-STRUCTURED-LOCATION"].params["X-TITLE"] = title

        event.add("uid", f"{page['id']}@notion2cal")
        event.add("dtstamp", datetime.now(timezone.utc))

        page_url = page.get("url")
        if page_url:
            event.add("url", page_url)

        cal.add_component(event)

    event_count = len([c for c in cal.subcomponents if c.name == "VEVENT"])
    print(
        f"Processed {len(pages)} pages: {event_count} events created, "
        f"{skipped} skipped (no date), {skipped_past} skipped (past)"
    )
    return cal


def main() -> None:
    if not NOTION_TOKEN:
        print("Error: NOTION_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    if not NOTION_DATABASE_ID:
        print(
            "Error: NOTION_DATABASE_ID environment variable is not set.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Querying Notion database {NOTION_DATABASE_ID[:8]}...")
    pages = query_database(NOTION_DATABASE_ID)
    print(f"Fetched {len(pages)} pages from Notion.")

    cal = build_calendar(pages)

    with open(OUTPUT_FILE, "wb") as f:
        f.write(cal.to_ical())

    print(f"Calendar written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
