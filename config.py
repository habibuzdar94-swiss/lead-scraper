"""Configuration for the real estate listing scraper.

Every setting has a default and can be overridden with an environment variable
named after the field in UPPER_CASE (for example ``TARGET_URL`` or
``MAX_PAGES``). Empty environment variables are ignored, which makes it safe
to pass optional GitHub Actions variables straight through.

CSS selector syntax used by the ``*_selector`` settings:
  * ``".price"``                 -> text of the first element matching ``.price``
  * ``"a@href"``                 -> the ``href`` attribute of the first ``a``
  * ``"@data-id"``               -> the ``data-id`` attribute of the card itself
  * ``"img@data-src || img@src"``-> alternatives, the first non-empty one wins
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from urllib.parse import urlparse

PLACEHOLDER_URL = "https://example.com/listings?page={page}"


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass
class Config:
    # ------------------------------------------------------------------ target
    # Use "{page}" in the URL for numbered pagination, e.g.
    # "https://site.com/search?type=sale&page={page}". Without "{page}" the
    # scraper follows the link found with NEXT_PAGE_SELECTOR instead.
    target_url: str = PLACEHOLDER_URL
    start_page: int = 1
    page_step: int = 1
    max_pages: int = 300
    max_listings: int = 5000
    min_listings: int = 1000
    stale_page_limit: int = 2            # stop after N pages without new listings
    max_consecutive_failures: int = 3    # stop after N pages that fail to download
    max_runtime_seconds: int = 3000      # graceful stop before the CI timeout
    next_page_selector: str = ""         # e.g. "a.next@href || a[rel=next]@href"

    # --------------------------------------------------------------- selectors
    listing_selector: str = (
        "article.listing, div.listing, li.listing, article.property-card, "
        "div.property-card, div.listing-card, [data-listing-id]"
    )
    id_selector: str = "@data-listing-id || @data-id"
    title_selector: str = "h2, h3, .title, .listing-title"
    url_selector: str = "a@href || @href"
    price_selector: str = ".price, .listing-price, [itemprop='price']"
    address_selector: str = ".address, .location, [itemprop='address']"
    property_type_selector: str = ".property-type, .type"
    bedrooms_selector: str = ".beds, .bedrooms"
    bathrooms_selector: str = ".baths, .bathrooms"
    area_selector: str = ".area, .size, .sqft"
    description_selector: str = ".description, .summary"
    image_selector: str = "img@data-src || img@src"
    use_json_ld: bool = True             # fall back to schema.org JSON-LD data

    # -------------------------------------------------------------------- HTTP
    user_agent: str = (
        "Mozilla/5.0 (compatible; RealEstateScraper/1.0; "
        "+https://github.com/habibuzdar94-swiss/lead-scraper)"
    )
    accept_language: str = "en-US,en;q=0.9"
    request_delay: float = 1.0           # polite delay between page requests
    timeout: float = 30.0
    max_retries: int = 4
    backoff_factor: float = 2.0
    verify_ssl: bool = True
    respect_robots: bool = True

    # ------------------------------------------------------------------ output
    output_dir: str = "output"
    output_basename: str = "listings"
    log_level: str = "INFO"
    strict: bool = False                 # exit non-zero if below min_listings

    def validate(self) -> None:
        """Raise ConfigError if the configuration cannot be used."""
        parsed = urlparse(self.target_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"TARGET_URL must be a full http(s) URL, got: {self.target_url!r}"
            )
        if self.target_url == PLACEHOLDER_URL or parsed.hostname in (
            "example.com",
            "www.example.com",
        ):
            raise ConfigError(
                "TARGET_URL is still the placeholder. Set the TARGET_URL "
                "environment variable (or GitHub Actions variable) to the "
                "listing page you want to scrape."
            )
        if self.max_retries < 1:
            raise ConfigError("MAX_RETRIES must be at least 1.")
        if self.max_pages < 1 or self.max_listings < 1:
            raise ConfigError("MAX_PAGES and MAX_LISTINGS must be at least 1.")
        if self.page_step < 1:
            raise ConfigError("PAGE_STEP must be at least 1.")
        if self.timeout <= 0:
            raise ConfigError("TIMEOUT must be greater than 0.")


_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}


def load_config() -> Config:
    """Build a Config from defaults overridden by environment variables."""
    config = Config()
    for f in fields(config):
        name = f.name.upper()
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            continue
        current = getattr(config, f.name)
        try:
            if isinstance(current, bool):
                lowered = raw.strip().lower()
                if lowered in _TRUE_VALUES:
                    value = True
                elif lowered in _FALSE_VALUES:
                    value = False
                else:
                    raise ValueError(raw)
            elif isinstance(current, int):
                value = int(raw.strip())
            elif isinstance(current, float):
                value = float(raw.strip())
            else:
                value = raw.strip()
        except ValueError as exc:
            raise ConfigError(f"Invalid value for {name}: {raw!r}") from exc
        setattr(config, f.name, value)
    return config
