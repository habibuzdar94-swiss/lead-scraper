"""Polite Realtor.com search-results scraper.

Reads Realtor.com SEARCH URLs from urls.txt, ignores non-search/detail/article URLs,
extracts embedded __NEXT_DATA__ listing objects first, then falls back to card HTML,
walks /pg-N pagination, de-duplicates records, and exports CSV + JSON.

This uses ordinary HTTP requests and respects robots.txt. Realtor.com may return
403/429 or an HTML shell to automated requests; this script intentionally does not
bypass access controls. If that happens, reduce request volume or use an authorized
data/API provider.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("realtor_scraper")
BASE_DOMAIN = "https://www.realtor.com"
SEARCH_MARKERS = ("/realestateandhomes-search/", "/apartments/")

@dataclass
class Listing:
    listing_id: str = ""
    price: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    bedrooms: str = ""
    bathrooms: str = ""
    sqft: str = ""
    lot_size: str = ""
    property_type: str = ""
    status: str = ""
    listing_date: str = ""
    agent_name: str = ""
    agent_phone: str = ""
    url: str = ""
    image_url: str = ""
    search_page_url: str = ""
    scraped_at: str = ""

    def key(self) -> str:
        return self.listing_id or self.url or "|".join((self.address, self.price, self.city, self.state))


def clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return re.sub(r"\s+", " ", str(value)).strip()


def nested(obj: Any, *path: str, default: Any = "") -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def search_seed(url: str) -> str | None:
    """Normalize a URL to its location-level search seed; reject detail/article URLs."""
    url = url.strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"www.realtor.com", "realtor.com"}:
        return None
    path = parsed.path.rstrip("/")
    if "/realestateandhomes-search/" in path:
        tail = path.split("/realestateandhomes-search/", 1)[1]
        location = tail.split("/", 1)[0]
        if not location or location.lower() == "near-me":
            # near-me depends on an interactive/current location and is not a reliable seed.
            return None
        return f"{BASE_DOMAIN}/realestateandhomes-search/{location}"
    if "/apartments/" in path:
        tail = path.split("/apartments/", 1)[1]
        location = tail.split("/", 1)[0]
        if not location:
            return None
        return f"{BASE_DOMAIN}/apartments/{location}"
    return None


def read_seeds(path: str = "urls.txt") -> list[str]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"Seed URL file not found: {file.resolve()}")
    seeds: list[str] = []
    for line in file.read_text(encoding="utf-8", errors="ignore").splitlines():
        seed = search_seed(line)
        if seed and seed not in seeds:
            seeds.append(seed)
    return seeds


def _address_parts(location: dict[str, Any]) -> tuple[str, str, str, str]:
    addr = location.get("address") or {}
    street = clean(addr.get("line") or addr.get("street_address") or addr.get("streetAddress"))
    city = clean(addr.get("city") or location.get("city"))
    state = clean(addr.get("state_code") or addr.get("state") or location.get("state_code"))
    postal = clean(addr.get("postal_code") or addr.get("postalCode") or location.get("postal_code"))
    full = ", ".join(x for x in (street, city, state, postal) if x)
    return full, city, state, postal


def _first_image(prop: dict[str, Any]) -> str:
    photo = prop.get("primary_photo") or prop.get("photo") or {}
    if isinstance(photo, dict):
        return clean(photo.get("href") or photo.get("url"))
    if isinstance(photo, list) and photo:
        first = photo[0]
        if isinstance(first, dict):
            return clean(first.get("href") or first.get("url"))
        return clean(first)
    photos = prop.get("photos")
    if isinstance(photos, list) and photos:
        first = photos[0]
        if isinstance(first, dict):
            return clean(first.get("href") or first.get("url"))
    return ""


def _listing_from_payload(prop: dict[str, Any], page_url: str) -> Listing | None:
    # Avoid interpreting arbitrary nested objects as listings.
    permalink = clean(prop.get("permalink") or prop.get("href"))
    property_id = clean(prop.get("property_id") or prop.get("listing_id") or prop.get("propertyId"))
    description = prop.get("description") if isinstance(prop.get("description"), dict) else {}
    location = prop.get("location") if isinstance(prop.get("location"), dict) else {}
    if not (property_id or permalink) or not ("list_price" in prop or "price" in prop or description or location):
        return None
    address, city, state, postal = _address_parts(location)
    if not address:
        address = clean(prop.get("address") or prop.get("full_address"))
    url = permalink
    if url and not url.startswith("http"):
        url = urljoin(BASE_DOMAIN + "/realestateandhomes-detail/", url.lstrip("/"))
    price_raw = prop.get("list_price", prop.get("price", ""))
    price = clean(price_raw)
    if price and price.replace(".", "", 1).isdigit():
        try:
            price = f"${int(float(price)):,}"
        except (ValueError, OverflowError):
            pass
    beds = description.get("beds", prop.get("beds", ""))
    baths = description.get("baths_consolidated", description.get("baths", prop.get("baths", "")))
    sqft = description.get("sqft", prop.get("sqft", ""))
    lot = description.get("lot_sqft", description.get("lot_size", prop.get("lot_sqft", "")))
    if isinstance(lot, dict):
        lot = lot.get("value", lot.get("size", ""))
    status = clean(prop.get("status"))
    listing_date = clean(prop.get("list_date") or prop.get("list_date_time"))
    agents = prop.get("advertisers") or prop.get("agents") or []
    agent_name = agent_phone = ""
    if isinstance(agents, list) and agents and isinstance(agents[0], dict):
        agent_name = clean(agents[0].get("name"))
        phones = agents[0].get("phones") or []
        if isinstance(phones, list) and phones and isinstance(phones[0], dict):
            agent_phone = clean(phones[0].get("number"))
    listing = Listing(
        listing_id=property_id,
        price=price,
        address=address,
        city=city,
        state=state,
        zip_code=postal,
        bedrooms=clean(beds),
        bathrooms=clean(baths),
        sqft=clean(sqft),
        lot_size=clean(lot),
        property_type=clean(prop.get("type") or prop.get("property_type")),
        status=status,
        listing_date=listing_date,
        agent_name=agent_name,
        agent_phone=agent_phone,
        url=url,
        image_url=_first_image(prop),
        search_page_url=page_url,
        scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return listing if listing.address or listing.price or listing.url else None


def _walk_payload(obj: Any, page_url: str) -> Iterable[Listing]:
    """Walk nested Next.js data and yield listing-like records."""
    if isinstance(obj, dict):
        item = _listing_from_payload(obj, page_url)
        if item:
            yield item
            # Don't walk children of a recognized property record; prevents sub-object noise.
            return
        for value in obj.values():
            yield from _walk_payload(value, page_url)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_payload(value, page_url)


def _text(card: Any, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        try:
            el = card.select_one(selector)
        except Exception:
            continue
        if el:
            value = clean(el.get("content") or el.get_text(" ", strip=True))
            if value:
                return value
    return ""


def parse_html(html: str, page_url: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    found: list[Listing] = []
    # Realtor.com commonly embeds search results in Next.js data.
    for script in soup.select('script#__NEXT_DATA__[type="application/json"], script#__NEXT_DATA__'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        found.extend(_walk_payload(data, page_url))
    if found:
        return _dedupe(found)

    # DOM fallback: data-testid names can change, so several known variants are tried.
    cards = soup.select('[data-testid="property-card"], [data-testid^="card-content-"], li[data-testid="result-card"], .component_property-card')
    for card in cards:
        link = card.select_one('a[data-testid="card-anchor"], a[data-testid="card-link"], a[data-card-link="true"], a[href*="realestateandhomes-detail"]')
        href = urljoin(page_url, link.get("href", "")) if link else ""
        address = _text(card, ('[data-testid="card-address"]', '[data-testid="card-address-1"]', '[data-label="pc-address"]', '.address'))
        city_state_zip = _text(card, ('[data-testid="card-address-2"]', '[data-label="pc-address"] .address-city', '.address-city'))
        if address and city_state_zip and city_state_zip not in address:
            address = f"{address}, {city_state_zip}"
        price = _text(card, ('[data-testid="card-price"]', '[data-label="pc-price"]', '[data-label="pc-price"] span'))
        beds = _text(card, ('[data-testid="property-meta-beds"]', '[data-testid="card-meta-beds"]', '[data-label="pc-meta-beds"]'))
        baths = _text(card, ('[data-testid="property-meta-baths"]', '[data-testid="card-meta-baths"]', '[data-label="pc-meta-baths"]'))
        sqft = _text(card, ('[data-testid="property-meta-sqft"]', '[data-testid="card-meta-sqft"]', '[data-label="pc-meta-sqft"]'))
        lot = _text(card, ('[data-testid="property-meta-lot-size"]', '[data-testid="card-meta-lot-size"]', '[data-label="pc-meta-lot-size"]'))
        image = card.select_one('img[data-testid="card-img"], img[src], img[data-src]')
        image_url = clean((image.get("src") or image.get("data-src")) if image else "")
        if not (href or address or price):
            continue
        pid = ""
        m = re.search(r"_([A-Z0-9-]+)$", urlparse(href).path)
        if m:
            pid = m.group(1)
        found.append(Listing(
            listing_id=pid, price=price, address=address, bedrooms=beds,
            bathrooms=baths, sqft=sqft, lot_size=lot, url=href,
            image_url=image_url, search_page_url=page_url,
            scraped_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ))
    return _dedupe(found)


def _dedupe(items: list[Listing]) -> list[Listing]:
    seen: set[str] = set()
    out: list[Listing] = []
    for item in items:
        key = item.key()
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def allowed_by_robots(session: requests.Session, url: str, user_agent: str) -> bool:
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = session.get(robots_url, timeout=20)
        if response.status_code != 200:
            LOG.warning("Could not read robots.txt (HTTP %s); stopping conservatively is not possible, continuing.", response.status_code)
            return True
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(user_agent, url)
    except requests.RequestException as exc:
        LOG.warning("robots.txt request failed (%s); continuing without a robots decision.", exc)
        return True


def make_page_url(seed: str, page: int) -> str:
    if page <= 1:
        return seed
    return f"{seed.rstrip('/')}/pg-{page}"


def scrape() -> list[Listing]:
    seeds = read_seeds(os.getenv("URLS_FILE", "urls.txt"))
    max_seeds = int(os.getenv("MAX_SEEDS", "60"))
    max_pages_per_seed = int(os.getenv("MAX_PAGES_PER_SEED", "5"))
    max_total_pages = int(os.getenv("MAX_TOTAL_PAGES", "200"))
    max_listings = int(os.getenv("MAX_LISTINGS", "5000"))
    delay = max(1.0, float(os.getenv("REQUEST_DELAY", "2.0")))
    user_agent = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; PropertyResearchBot/1.0; +https://www.realtor.com/terms-of-service)")
    seeds = seeds[:max_seeds]
    LOG.info("Loaded %d unique search seeds; processing %d (MAX_SEEDS=%d).", len(read_seeds(os.getenv("URLS_FILE", "urls.txt"))), len(seeds), max_seeds)
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    all_rows: dict[str, Listing] = {}
    pages_done = 0
    robots_checked = False
    for seed_index, seed in enumerate(seeds, 1):
        LOG.info("Seed %d/%d: %s", seed_index, len(seeds), seed)
        for page_num in range(1, max_pages_per_seed + 1):
            if pages_done >= max_total_pages or len(all_rows) >= max_listings:
                break
            url = make_page_url(seed, page_num)
            if not robots_checked:
                robots_checked = True
                if not allowed_by_robots(session, url, user_agent):
                    LOG.error("robots.txt disallows this search URL. Stopping; no bypass attempted: %s", url)
                    return list(all_rows.values())
            if pages_done:
                time.sleep(delay + random.uniform(0, 0.5))
            try:
                response = session.get(url, timeout=30)
                pages_done += 1
                if response.status_code in (403, 429):
                    LOG.warning("HTTP %s from Realtor.com at %s; stopping to avoid repeated requests.", response.status_code, url)
                    return list(all_rows.values())
                response.raise_for_status()
            except requests.RequestException as exc:
                LOG.warning("Request failed for %s: %s", url, exc)
                break
            page_rows = parse_html(response.text, url)
            new_count = 0
            for row in page_rows:
                if row.key() not in all_rows:
                    all_rows[row.key()] = row
                    new_count += 1
                    if len(all_rows) >= max_listings:
                        break
            LOG.info("Page %d: parsed %d records, %d new, %d unique total.", page_num, len(page_rows), new_count, len(all_rows))
            # Don't walk empty/non-listing pages repeatedly.
            if not page_rows or (page_num > 1 and new_count == 0):
                break
        if pages_done >= max_total_pages or len(all_rows) >= max_listings:
            break
    LOG.info("Finished: pages=%d unique_listings=%d", pages_done, len(all_rows))
    return list(all_rows.values())


def export(rows: list[Listing]) -> None:
    out_dir = Path(os.getenv("OUTPUT_DIR", "output"))
    out_dir.mkdir(parents=True, exist_ok=True)
    basename = os.getenv("OUTPUT_BASENAME", "listings")
    records = [asdict(row) for row in rows]
    csv_path = out_dir / f"{basename}.csv"
    json_path = out_dir / f"{basename}.json"
    columns = list(Listing.__dataclass_fields__.keys())
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
    json_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    LOG.info("Wrote %d listings to %s and %s", len(rows), csv_path, json_path)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    try:
        rows = scrape()
        export(rows)
    except Exception:
        LOG.exception("Scraper failed")
        return 1
    if not rows:
        LOG.error("No listings extracted. Realtor.com may have returned a blocked page or changed its data structure. Check the Actions log.")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
