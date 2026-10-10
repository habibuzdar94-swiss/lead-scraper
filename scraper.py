"""Real estate listing scraper.

Collects listings from a paginated website, extracts property details,
removes duplicates, and exports CSV and JSON files. All site-specific
behaviour (URL, pagination, CSS selectors) lives in config.py / environment
variables.

Exit codes:
  0  success
  1  no listings were collected
  2  invalid configuration
  3  blocked by robots.txt
  4  fewer than MIN_LISTINGS collected and --strict / STRICT is enabled
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from config import Config, ConfigError, load_config

logger = logging.getLogger("scraper")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
MAX_RETRY_AFTER_SECONDS = 120
MAX_DESCRIPTION_LENGTH = 2000

CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
    "₨": "PKR", "₽": "RUB", "₺": "TRY", "₩": "KRW",
}
CURRENCY_CODE_RE = re.compile(
    r"\b(USD|EUR|GBP|CHF|CAD|AUD|AED|PKR|INR|SAR|JPY|CNY|SEK|NOK|DKK|PLN|CZK|"
    r"TRY|MXN|BRL|ZAR|SGD|HKD|NZD)\b"
)
TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "ref", "ref_src", "mc_cid", "mc_eid"}

LISTING_TYPES = {
    "realestatelisting", "residence", "house", "apartment",
    "singlefamilyresidence", "accommodation", "product",
}
RESIDENCE_TYPES = {"house", "apartment", "singlefamilyresidence"}


class RobotsDisallowedError(RuntimeError):
    """Raised when robots.txt forbids fetching the target URL."""


# --------------------------------------------------------------------------- #
# Text / number helpers
# --------------------------------------------------------------------------- #
def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _tidy(value: Optional[float]) -> Optional[float]:
    """Return ints for whole numbers so exports look clean."""
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _normalise_numeric(token: str) -> Optional[float]:
    token = token.strip().replace(" ", "").replace("\u00a0", "").strip(".,")
    if not token:
        return None
    has_dot, has_comma = "." in token, "," in token
    if has_dot and has_comma:
        if token.rfind(",") > token.rfind("."):  # 1.234,56
            token = token.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            token = token.replace(",", "")
    elif has_comma:
        if re.fullmatch(r"\d{1,3}(,\d{3})+", token):
            token = token.replace(",", "")
        else:
            token = token.replace(",", ".")
    elif has_dot and token.count(".") > 1:
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", token):
            token = token.replace(".", "")
        else:
            return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_number(text: Any) -> Optional[float]:
    """Return the first number found in ``text`` (or None)."""
    if text is None:
        return None
    cleaned = str(text).replace("'", "").replace("’", "")
    match = re.search(r"\d[\d.,]*\d|\d", cleaned)
    if not match:
        return None
    return _tidy(_normalise_numeric(match.group(0)))


def detect_currency(text: str) -> str:
    match = CURRENCY_CODE_RE.search(text)
    if match:
        return match.group(1)
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return ""


def parse_price(text: Any) -> Tuple[Optional[float], str]:
    """Return ``(value, currency)`` parsed from a price string."""
    if not text:
        return None, ""
    raw = str(text)
    currency = detect_currency(raw)
    cleaned = raw.replace("'", "").replace("’", "")
    cleaned = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))", "", cleaned)
    match = re.search(r"(\d[\d.,]*\d|\d)\s*([kKmM])?(?![a-zA-Z])", cleaned)
    if not match:
        return None, currency
    value = _normalise_numeric(match.group(1))
    if value is None:
        return None, currency
    suffix = (match.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return _tidy(value), currency


def normalize_url(url: str) -> str:
    """Normalise a URL for duplicate detection."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    query.sort()
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query), "")
    )


def id_from_url(url: str) -> str:
    if not url:
        return ""
    numbers = re.findall(r"\d{4,}", urlparse(url).path)
    return numbers[-1] if numbers else ""


# --------------------------------------------------------------------------- #
# CSS selector helpers
# --------------------------------------------------------------------------- #
def _select_one(node: Any, selector: str) -> str:
    selector = selector.strip()
    if not selector:
        return ""
    css, attr = selector, None
    if "@" in selector:
        css, attr = selector.rsplit("@", 1)
        css, attr = css.strip(), attr.strip()
    target = node.select_one(css) if css else node
    if target is None:
        return ""
    if attr:
        value = target.get(attr, "")
        if isinstance(value, (list, tuple)):
            value = " ".join(value)
        return clean_text(value)
    return clean_text(target.get_text(" ", strip=True))


def select_value(node: Any, selector: str) -> str:
    """Extract text/attribute using the selector mini-syntax from config.py."""
    if not selector:
        return ""
    for alternative in selector.split("||"):
        value = _select_one(node, alternative)
        if value:
            return value
    return ""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Listing:
    listing_id: str = ""
    title: str = ""
    price: str = ""
    price_value: Optional[float] = None
    currency: str = ""
    address: str = ""
    property_type: str = ""
    bedrooms: Optional[float] = None
    bathrooms: Optional[float] = None
    area: str = ""
    area_value: Optional[float] = None
    description: str = ""
    url: str = ""
    image_url: str = ""
    source_page: str = ""
    scraped_at: str = ""

    def dedupe_key(self) -> str:
        if self.url:
            return "url:" + normalize_url(self.url)
        if self.listing_id:
            return "id:" + self.listing_id
        raw = "|".join(x.lower().strip() for x in (self.title, self.address, self.price))
        return "hash:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def is_meaningful(self) -> bool:
        return bool(self.title or self.url or self.price or self.address)


# --------------------------------------------------------------------------- #
# JSON-LD helpers
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _types_of(node: Dict[str, Any]) -> set:
    return {
        str(t).lower().split("/")[-1].split(":")[-1]
        for t in _as_list(node.get("@type"))
    }


def iter_json_ld_items(data: Any) -> Iterator[Dict[str, Any]]:
    """Yield listing-like objects from parsed JSON-LD."""
    if isinstance(data, list):
        for element in data:
            yield from iter_json_ld_items(element)
        return
    if not isinstance(data, dict):
        return
    if "@graph" in data:
        yield from iter_json_ld_items(data["@graph"])
    types = _types_of(data)
    if types & LISTING_TYPES:
        yield data
    elif "itemlist" in types:
        for element in _as_list(data.get("itemListElement")):
            if not isinstance(element, dict):
                continue
            item = element.get("item", element)
            if isinstance(item, str):
                item = {"url": item}
            if isinstance(item, dict) and (
                _types_of(item) & LISTING_TYPES or item.get("url") or item.get("name")
            ):
                yield item


def _json_number(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _tidy(float(value))
    return parse_number(value)


def _first_url(value: Any) -> str:
    for element in _as_list(value):
        if isinstance(element, str) and element:
            return element
        if isinstance(element, dict):
            candidate = element.get("url") or element.get("contentUrl")
            if candidate:
                return str(candidate)
    return ""


def _format_address(value: Any) -> str:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        if "address" in value and "streetAddress" not in value:
            return _format_address(value["address"])
        parts = []
        for key in ("streetAddress", "addressLocality", "addressRegion",
                    "postalCode", "addressCountry"):
            part = value.get(key)
            if isinstance(part, dict):
                part = part.get("name")
            if part:
                parts.append(clean_text(part))
        return ", ".join(parts)
    return ""


# --------------------------------------------------------------------------- #
# Scraper
# --------------------------------------------------------------------------- #
class Scraper:
    def __init__(self, config: Config, session: Optional[Any] = None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": config.accept_language,
            }
        )
        self._robots: Dict[str, Optional[RobotFileParser]] = {}
        self._run_timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.stats: Dict[str, int] = {
            "pages_fetched": 0,
            "pages_failed": 0,
            "listings_seen": 0,
            "duplicates_skipped": 0,
        }

    # ------------------------------------------------------------ robots.txt
    def _load_robots(self, origin: str) -> Optional[RobotFileParser]:
        robots_url = origin + "/robots.txt"
        try:
            response = self.session.get(
                robots_url, timeout=self.config.timeout, verify=self.config.verify_ssl
            )
        except requests.RequestException as exc:
            logger.warning("Could not fetch %s (%s); assuming allowed.", robots_url, exc)
            return None
        if response.status_code != 200:
            logger.info("No usable robots.txt at %s (HTTP %s).", robots_url, response.status_code)
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        parser.modified()
        return parser

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch("*", url)

    # ------------------------------------------------------------ networking
    def _backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        delay = self.config.backoff_factor * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        if retry_after and str(retry_after).strip().isdigit():
            delay = max(delay, min(int(retry_after), MAX_RETRY_AFTER_SECONDS))
        logger.info("Waiting %.1fs before retrying (attempt %d)...", delay, attempt)
        time.sleep(delay)

    @staticmethod
    def _decode(response: Any) -> str:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if "charset" not in content_type:
            response.encoding = getattr(response, "apparent_encoding", None) or "utf-8"
        return response.text

    def fetch(self, url: str) -> Optional[str]:
        """Download ``url`` with retries. Returns HTML text or None on failure."""
        cfg = self.config
        for attempt in range(1, cfg.max_retries + 1):
            try:
                response = self.session.get(
                    url, timeout=cfg.timeout, verify=cfg.verify_ssl
                )
            except requests.RequestException as exc:
                logger.warning("Request error for %s (attempt %d/%d): %s",
                               url, attempt, cfg.max_retries, exc)
                if attempt == cfg.max_retries:
                    break
                self._backoff(attempt)
                continue

            status = response.status_code
            if 200 <= status < 300:
                return self._decode(response)
            if status in (404, 410):
                logger.info("HTTP %s for %s (end of results).", status, url)
                return None
            if status in RETRYABLE_STATUS:
                logger.warning("HTTP %s for %s (attempt %d/%d).",
                               status, url, attempt, cfg.max_retries)
                if attempt == cfg.max_retries:
                    break
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            logger.error("Non-retryable HTTP %s for %s.", status, url)
            return None

        logger.error("Giving up on %s after %d attempts.", url, cfg.max_retries)
        return None

    def _polite_delay(self) -> None:
        delay = self.config.request_delay
        if delay > 0:
            time.sleep(delay + random.uniform(0, delay * 0.3))

    # --------------------------------------------------------------- parsing
    def _listing_from_card(self, card: Any, page_url: str) -> Listing:
        cfg = self.config
        href = select_value(card, cfg.url_selector)
        url = ""
        if href and not href.lower().startswith(("javascript:", "#")):
            url = urljoin(page_url, href)
        price_text = select_value(card, cfg.price_selector)
        price_value, currency = parse_price(price_text)
        area_text = select_value(card, cfg.area_selector)
        image = select_value(card, cfg.image_selector)
        return Listing(
            listing_id=select_value(card, cfg.id_selector) or id_from_url(url),
            title=select_value(card, cfg.title_selector),
            price=price_text,
            price_value=price_value,
            currency=currency,
            address=select_value(card, cfg.address_selector),
            property_type=select_value(card, cfg.property_type_selector),
            bedrooms=parse_number(select_value(card, cfg.bedrooms_selector)),
            bathrooms=parse_number(select_value(card, cfg.bathrooms_selector)),
            area=area_text,
            area_value=parse_number(area_text),
            description=select_value(card, cfg.description_selector)[:MAX_DESCRIPTION_LENGTH],
            url=url,
            image_url=urljoin(page_url, image) if image else "",
            source_page=page_url,
            scraped_at=self._run_timestamp,
        )

    def _listing_from_json_ld(self, item: Dict[str, Any], page_url: str) -> Listing:
        offers = item.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if not isinstance(offers, dict):
            offers = {}
        offered = offers.get("itemOffered")
        if not isinstance(offered, dict):
            offered = {}

        def pick(*keys: str) -> Any:
            for source in (item, offered):
                for key in keys:
                    value = source.get(key)
                    if value not in (None, "", []):
                        return value
            return None

        price_raw = offers.get("price")
        if price_raw in (None, ""):
            spec = offers.get("priceSpecification")
            if isinstance(spec, dict):
                price_raw = spec.get("price")
        if price_raw in (None, ""):
            price_raw = item.get("price")

        currency = clean_text(offers.get("priceCurrency"))
        if isinstance(price_raw, (int, float)) and not isinstance(price_raw, bool):
            price_value: Optional[float] = _tidy(float(price_raw))
            price_text = f"{currency} {price_raw}".strip()
        else:
            price_text = clean_text(price_raw)
            price_value, detected = parse_price(price_text)
            currency = currency or detected
            if currency and price_text and currency not in price_text:
                price_text = f"{currency} {price_text}"

        area_raw = pick("floorSize")
        if isinstance(area_raw, dict):
            area_value = _json_number(area_raw)
            area_text = clean_text(f"{area_raw.get('value', '')} {area_raw.get('unitText', '')}")
        else:
            area_text = clean_text(area_raw)
            area_value = parse_number(area_text)

        raw_url = item.get("url") or offers.get("url") or ""
        if not raw_url:
            at_id = str(item.get("@id", ""))
            raw_url = at_id if at_id.startswith(("http", "/")) else ""
        url = urljoin(page_url, str(raw_url)) if raw_url else ""

        property_type = clean_text(pick("accommodationCategory", "propertyType"))
        if not property_type:
            for t in _as_list(item.get("@type")):
                if str(t).lower() in RESIDENCE_TYPES:
                    property_type = str(t)
                    break

        image = _first_url(pick("image", "photo"))
        return Listing(
            listing_id=clean_text(pick("identifier", "sku", "productID")) or id_from_url(url),
            title=clean_text(pick("name", "headline")),
            price=price_text,
            price_value=price_value,
            currency=currency,
            address=_format_address(pick("address", "location")),
            property_type=property_type,
            bedrooms=_json_number(pick("numberOfBedrooms", "numberOfRooms")),
            bathrooms=_json_number(
                pick("numberOfBathroomsTotal", "numberOfFullBathrooms", "numberOfBathrooms")
            ),
            area=area_text,
            area_value=area_value,
            description=clean_text(pick("description"))[:MAX_DESCRIPTION_LENGTH],
            url=url,
            image_url=urljoin(page_url, image) if image else "",
            source_page=page_url,
            scraped_at=self._run_timestamp,
        )

    def _listings_from_json_ld(self, soup: BeautifulSoup, page_url: str) -> List[Listing]:
        listings: List[Listing] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("Skipping invalid JSON-LD block.")
                continue
            for item in iter_json_ld_items(data):
                listing = self._listing_from_json_ld(item, page_url)
                if listing.is_meaningful():
                    listings.append(listing)
        return listings

    def _parse_soup(self, soup: BeautifulSoup, page_url: str) -> List[Listing]:
        try:
            cards = soup.select(self.config.listing_selector)
             except Exception as exc:
            raise ConfigError(
                f"Invalid LISTING_SELECTOR {self.config.listing_selector!r}: {exc}"
            ) from exc
        listings = []
        for card in cards:
            listing = self._listing_from_card(card, page_url)
            if listing.is_meaningful():
                listings.append(listing)
        if not listings and self.config.use_json_ld:
            listings = self._listings_from_json_ld(soup, page_url)
        return listings

    def parse_page(self, html: str, page_url: str) -> List[Listing]:
        """Parse listings out of one HTML page."""
        return self._parse_soup(BeautifulSoup(html, "lxml"), page_url)

    # ------------------------------------------------------------ pagination
    def _page_url(self, page_number: int) -> str:
        return self.config.target_url.replace("{page}", str(page_number))

    def first_url(self) -> str:
        if "{page}" in self.config.target_url:
            return self._page_url(self.config.start_page)
        return self.config.target_url

    def find_next_page(
        self, soup: Optional[BeautifulSoup], current_url: str, page_index: int
    ) -> Optional[str]:
        cfg = self.config
        if cfg.next_page_selector:
            if soup is None:
                return None
            href = select_value(soup, cfg.next_page_selector)
            if not href or href.lower().startswith(("javascript:", "#")):
                return None
            return urljoin(current_url, href)
        if "{page}" in cfg.target_url:
            return self._page_url(cfg.start_page + (page_index + 1) * cfg.page_step)
        return None

    # ------------------------------------------------------------------- run
    def run(self) -> List[Listing]:
        cfg = self.config
        unique: Dict[str, Listing] = {}
        visited = set()
        url: Optional[str] = self.first_url()
        stale = 0
        failures = 0
        started = time.monotonic()

        for page_index in range(cfg.max_pages):
            if not url or len(unique) >= cfg.max_listings:
                break
            if time.monotonic() - started > cfg.max_runtime_seconds:
                logger.warning("Runtime limit reached; stopping with partial results.")
                break
            if url in visited:
                logger.info("Already visited %s; stopping pagination.", url)
                break
            visited.add(url)

            if cfg.respect_robots and not self.is_allowed(url):
                raise RobotsDisallowedError(f"robots.txt disallows fetching {url}")
            if page_index > 0:
                self._polite_delay()

            html = self.fetch(url)
            if html is None:
                failures += 1
                self.stats["pages_failed"] += 1
                if failures >= cfg.max_consecutive_failures:
                    logger.error("Too many consecutive failures; stopping.")
                    break
                url = self.find_next_page(None, url, page_index)
                continue

            failures = 0
            self.stats["pages_fetched"] += 1
            soup = BeautifulSoup(html, "lxml")
            page_listings = self._parse_soup(soup, url)
            self.stats["listings_seen"] += len(page_listings)

            new = 0
            for listing in page_listings:
                key = listing.dedupe_key()
                if key in unique:
                    self.stats["duplicates_skipped"] += 1
                    continue
                unique[key] = listing
                new += 1
                if len(unique) >= cfg.max_listings:
                    break

            logger.info("Page %d: %d found, %d new, %d total unique.",
                        page_index + 1, len(page_listings), new, len(unique))

            stale = 0 if new else stale + 1
            if stale >= cfg.stale_page_limit:
                logger.info("No new listings on %d consecutive pages; stopping.", stale)
                break
            url = self.find_next_page(soup, url, page_index)

        return list(unique.values())


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def export_listings(
    listings: List[Listing], output_dir: str, basename: str = "listings"
) -> Dict[str, Path]:
    """Write listings to CSV and JSON files. Returns the created paths."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    columns = [f.name for f in fields(Listing)]
    rows = [asdict(listing) for listing in listings]

    csv_path = directory / f"{basename}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    json_path = directory / f"{basename}.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)

    return {"csv": csv_path, "json": json_path}


def write_step_summary(listings: List[Listing], stats: Dict[str, int]) -> None:
    """Append a short summary to the GitHub Actions job summary if available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("## Scrape summary\n\n")
            handle.write(f"- Unique listings: **{len(listings)}**\n")
            for key, value in stats.items():
                handle.write(f"- {key.replace('_', ' ').capitalize()}: {value}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real estate listing scraper.")
    parser.add_argument("--url", help="Override TARGET_URL.")
    parser.add_argument("--max-listings", type=int, help="Override MAX_LISTINGS.")
    parser.add_argument("--max-pages", type=int, help="Override MAX_PAGES.")
    parser.add_argument("--output-dir", help="Override OUTPUT_DIR.")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR.")
    parser.add_argument("--strict", action="store_true",
                        help="Exit with code 4 if fewer than MIN_LISTINGS are collected.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config()
        overrides: Dict[str, Any] = {}
        if args.url:
            overrides["target_url"] = args.url
        if args.max_listings:
            overrides["max_listings"] = args.max_listings
        if args.max_pages:
            overrides["max_pages"] = args.max_pages
        if args.output_dir:
            overrides["output_dir"] = args.output_dir
        if args.log_level:
            overrides["log_level"] = args.log_level
        if args.strict:
            overrides["strict"] = True
        config = replace(config, **overrides)
        logging.basicConfig(
            level=getattr(logging, config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(message)s",
        )
        config.validate()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        logger.error("Configuration error: %s", exc)
        return 2

    scraper = Scraper(config)
    try:
        listings = scraper.run()
    except RobotsDisallowedError as exc:
        logger.error("%s", exc)
        return 3
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        return 2

    logger.info("Stats: %s", scraper.stats)
    write_step_summary(listings, scraper.stats)

    if not listings:
        logger.error("No listings were collected. Check TARGET_URL and the CSS selectors.")
        return 1

    paths = export_listings(listings, config.output_dir, config.output_basename)
    logger.info("Exported %d listings to %s and %s.",
                len(listings), paths["csv"], paths["json"])

    if len(listings) < config.min_listings:
        logger.warning("Collected %d listings, below the target of %d.",
                       len(listings), config.min_listings)
        if config.strict:
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
