"""Offline tests for the listing scraper (no network access required)."""
import csv
import json

import pytest
import requests
from bs4 import BeautifulSoup

import scraper
from config import Config, ConfigError, load_config
from scraper import (
    Listing,
    RobotsDisallowedError,
    Scraper,
    export_listings,
    normalize_url,
    parse_number,
    parse_price,
)

BASE = "https://example.com/listings?page={page}"


def make_config(**kwargs):
    defaults = dict(target_url=BASE, respect_robots=False, request_delay=0.0)
    defaults.update(kwargs)
    return Config(**defaults)


class FakeResponse:
    def __init__(self, status_code=200, text="", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


class FakeSession:
    """Returns queued responses per URL; the last queued item repeats forever."""

    def __init__(self, routes=None):
        self.routes = {
            url: (list(items) if isinstance(items, list) else [items])
            for url, items in (routes or {}).items()
        }
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None, verify=True, **kwargs):
        self.calls.append(url)
        queue = self.routes.get(url)
        if not queue:
            return FakeResponse(404, "")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


CARD_HTML = """
<html><body>
<article class="listing" data-listing-id="A1">
  <h2>Sunny Villa</h2>
  <a href="/property/a1?utm_source=x">Details</a>
  <span class="price">CHF 1'250'000</span>
  <span class="address">Bahnhofstrasse 1, Zurich</span>
  <span class="property-type">Villa</span>
  <span class="beds">4 beds</span>
  <span class="baths">2.5 baths</span>
  <span class="area">180 m²</span>
  <p class="description">Bright and spacious.</p>
  <img data-src="/img/a1.jpg" src="/placeholder.gif">
</article>
</body></html>
"""

JSON_LD_HTML = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{
   "@type":"House","name":"Lake House","url":"/p/1","description":"Nice view",
   "image":"/i/1.jpg","numberOfBedrooms":3,"numberOfBathroomsTotal":2,
   "floorSize":{"@type":"QuantitativeValue","value":150,"unitText":"m2"},
   "address":{"@type":"PostalAddress","streetAddress":"1 Lake Rd",
              "addressLocality":"Lucerne","addressCountry":"CH"},
   "offers":{"@type":"Offer","price":"950000","priceCurrency":"CHF"}}}
]}
</script></head><body></body></html>
"""


def make_page(page, per_page=45, step=40):
    first = (page - 1) * step + 1
    cards = "".join(
        f'<article class="listing" data-listing-id="{i}"><h2>House {i}</h2>'
        f'<a href="/property/{i}">view</a><span class="price">${100000 + i:,}</span>'
        f"</article>"
        for i in range(first, first + per_page)
    )
    return f"<html><body>{cards}</body></html>"


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize(
    "text, value, currency",
    [
        ("$1,250,000", 1250000, "USD"),
        ("CHF 1'250'000.–", 1250000, "CHF"),
        ("€ 1.250.000,50", 1250000.5, "EUR"),
        ("£850k", 850000, "GBP"),
        ("1.2M USD", 1200000, "USD"),
        ("1 250 000 CHF", 1250000, "CHF"),
        ("Price on request", None, ""),
        ("", None, ""),
    ],
)
def test_parse_price(text, value, currency):
    assert parse_price(text) == (value, currency)


@pytest.mark.parametrize(
    "text, expected",
    [("3 beds", 3), ("2.5 baths", 2.5), ("1,200 sq ft", 1200), ("n/a", None), (None, None)],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


def test_normalize_url_strips_tracking_and_fragments():
    a = normalize_url("https://Example.com/a/?utm_source=x&id=5#frag")
    b = normalize_url("https://example.com/a?id=5")
    assert a == b


# ------------------------------------------------------------------ parsing
def test_parse_page_from_css_cards():
    s = Scraper(make_config(), session=FakeSession())
    listings = s.parse_page(CARD_HTML, "https://example.com/listings?page=1")
    assert len(listings) == 1
    item = listings[0]
    assert item.listing_id == "A1"
    assert item.title == "Sunny Villa"
    assert item.url == "https://example.com/property/a1?utm_source=x"
    assert item.price_value == 1250000
    assert item.currency == "CHF"
    assert item.address == "Bahnhofstrasse 1, Zurich"
    assert item.property_type == "Villa"
    assert item.bedrooms == 4
    assert item.bathrooms == 2.5
    assert item.area_value == 180
    assert item.image_url == "https://example.com/img/a1.jpg"


def test_parse_page_json_ld_fallback():
    s = Scraper(make_config(), session=FakeSession())
    listings = s.parse_page(JSON_LD_HTML, "https://example.com/listings?page=1")
    assert len(listings) == 1
    item = listings[0]
    assert item.title == "Lake House"
    assert item.url == "https://example.com/p/1"
    assert item.price_value == 950000
    assert item.currency == "CHF"
    assert item.bedrooms == 3
    assert item.bathrooms == 2
    assert item.area_value == 150
    assert item.address == "1 Lake Rd, Lucerne, CH"
    assert item.image_url == "https://example.com/i/1.jpg"


def test_invalid_selector_raises_config_error():
    s = Scraper(make_config(listing_selector="div["), session=FakeSession())
    with pytest.raises(ConfigError):
        s.parse_page("<html></html>", "https://example.com/")


# --------------------------------------------------------------- pagination
def test_find_next_page_with_selector():
    cfg = make_config(target_url="https://example.com/list", next_page_selector="a.next@href")
    s = Scraper(cfg, session=FakeSession())
    soup = BeautifulSoup('<a class="next" href="?page=2">Next</a>', "lxml")
    assert s.find_next_page(soup, "https://example.com/list", 0) == "https://example.com/list?page=2"
    assert s.find_next_page(BeautifulSoup("<p>none</p>", "lxml"), "https://example.com/list", 0) is None


def test_find_next_page_with_template():
    s = Scraper(make_config(), session=FakeSession())
    soup = BeautifulSoup("<html></html>", "lxml")
    assert s.first_url() == "https://example.com/listings?page=1"
    assert s.find_next_page(soup, s.first_url(), 0) == "https://example.com/listings?page=2"
    assert s.find_next_page(soup, "x", 4) == "https://example.com/listings?page=6"


def test_run_collects_over_1000_unique_listings_with_duplicates_removed():
    s = Scraper(make_config(), session=FakeSession())

    def fake_fetch(url):
        page = int(url.rsplit("=", 1)[1])
        return make_page(page) if page <= 30 else "<html><body></body></html>"

    s.fetch = fake_fetch
    result = s.run()
    assert len(result) == 29 * 40 + 45 == 1205
    assert len({item.url for item in result}) == len(result)
    assert s.stats["duplicates_skipped"] == 29 * 5


def test_run_respects_max_listings():
    s = Scraper(make_config(max_listings=100), session=FakeSession())
    s.fetch = lambda url: make_page(int(url.rsplit("=", 1)[1]))
    assert len(s.run()) == 100


def test_run_stops_after_stale_pages():
    s = Scraper(make_config(stale_page_limit=2), session=FakeSession())
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return make_page(1)

    s.fetch = fake_fetch
    result = s.run()
    assert len(result) == 45
    assert len(calls) == 3


def test_run_follows_next_links():
    cfg = make_config(target_url="https://example.com/list", next_page_selector="a.next@href")
    s = Scraper(cfg, session=FakeSession())

    def card(i):
        return f'<article class="listing"><h2>H{i}</h2><a href="/p/{i}">x</a></article>'

    pages = {
        "https://example.com/list": "<html>" + card(1) + card(2) + card(3)
        + '<a class="next" href="/list?page=2">n</a></html>',
        "https://example.com/list?page=2": "<html>" + card(3) + card(4) + card(5) + "</html>",
    }
    s.fetch = lambda url: pages.get(url)
    result = s.run()
    assert [item.title for item in result] == ["H1", "H2", "H3", "H4", "H5"]


def test_run_stops_after_consecutive_failures():
    s = Scraper(make_config(max_consecutive_failures=3), session=FakeSession())
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return None

    s.fetch = fake_fetch
    assert s.run() == []
    assert len(calls) == 3


# ----------------------------------------------------------- errors / retries
def test_fetch_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: None)
    url = "https://example.com/x"
    session = FakeSession(
        {url: [FakeResponse(500), FakeResponse(503, headers={"Retry-After": "1"}),
               FakeResponse(200, "<html>ok</html>")]}
    )
    s = Scraper(make_config(max_retries=4), session=session)
    assert s.fetch(url) == "<html>ok</html>"
    assert len(session.calls) == 3


def test_fetch_retries_on_connection_error(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: None)
    url = "https://example.com/x"
    session = FakeSession({url: [requests.ConnectionError("boom"), FakeResponse(200, "ok")]})
    s = Scraper(make_config(max_retries=3), session=session)
    assert s.fetch(url) == "ok"
    assert len(session.calls) == 2


def test_fetch_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: None)
    url = "https://example.com/x"
    session = FakeSession({url: [FakeResponse(500)]})
    s = Scraper(make_config(max_retries=3), session=session)
    assert s.fetch(url) is None
    assert len(session.calls) == 3


def test_fetch_does_not_retry_404(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: None)
    url = "https://example.com/missing"
    session = FakeSession({url: [FakeResponse(404)]})
    s = Scraper(make_config(max_retries=4), session=session)
    assert s.fetch(url) is None
    assert len(session.calls) == 1


def test_robots_txt_disallow_blocks_run():
    session = FakeSession(
        {"https://example.com/robots.txt":
         FakeResponse(200, "User-agent: *\nDisallow: /listings")}
    )
    s = Scraper(make_config(respect_robots=True), session=session)
    with pytest.raises(RobotsDisallowedError):
        s.run()


# -------------------------------------------------------------------- export
def test_export_csv_and_json(tmp_path):
    listings = [
        Listing(listing_id="1", title="A, with comma", price="$1", price_value=1,
                currency="USD", url="https://x.test/1"),
        Listing(listing_id="2", title="Château Élégant", url="https://x.test/2",
                bedrooms=5, bathrooms=2.5),
    ]
    paths = export_listings(listings, str(tmp_path / "out"), "data")

    with paths["csv"].open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert rows[0]["title"] == "A, with comma"
    assert rows[1]["title"] == "Château Élégant"

    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert [row["listing_id"] for row in data] == ["1", "2"]
    assert data[1]["bathrooms"] == 2.5


# -------------------------------------------------------------------- config
def test_load_config_reads_environment(monkeypatch):
    monkeypatch.setenv("TARGET_URL", "https://site.test/l?page={page}")
    monkeypatch.setenv("MAX_PAGES", "7")
    monkeypatch.setenv("RESPECT_ROBOTS", "false")
    monkeypatch.setenv("REQUEST_DELAY", "0.5")
    monkeypatch.setenv("LISTING_SELECTOR", "")
    cfg = load_config()
    assert cfg.target_url == "https://site.test/l?page={page}"
    assert cfg.max_pages == 7
    assert cfg.respect_robots is False
    assert cfg.request_delay == 0.5
    assert cfg.listing_selector == Config().listing_selector


def test_load_config_rejects_bad_values(monkeypatch):
    monkeypatch.setenv("MAX_PAGES", "many")
    with pytest.raises(ConfigError):
        load_config()


def test_validate_rejects_placeholder_and_bad_urls():
    with pytest.raises(ConfigError):
        Config().validate()
    with pytest.raises(ConfigError):
        Config(target_url="ftp://site.test/x").validate()
    Config(target_url="https://site.test/l?page={page}").validate()
