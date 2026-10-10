# lead-scraper

A configurable real estate listing scraper. It collects 1,000+ listings when the
site has them, follows pagination, extracts property details, removes duplicates,
retries failed requests, and exports **CSV** and **JSON**. It runs entirely in
the cloud through **GitHub Actions**, so no personal computer is needed.

## What it extracts

`listing_id`, `title`, `price`, `price_value`, `currency`, `address`,
`property_type`, `bedrooms`, `bathrooms`, `area`, `area_value`, `description`,
`url`, `image_url`, `source_page`, `scraped_at`

## Run it in GitHub Actions (no local setup)

1. Open **Settings → Secrets and variables → Actions → Variables** and add:
   - `TARGET_URL` – the listing/search page. Use `{page}` for numbered pages,
     e.g. `https://yoursite.com/search?type=sale&page={page}`.
   - Optionally the CSS selector variables below (`LISTING_SELECTOR`, `PRICE_SELECTOR`, ...).
2. Open **Actions → Scrape listings → Run workflow**. It also runs daily at 03:00 UTC.
   To scrape a different page for one run, paste its URL in the **Listing URL** box.
3. Download the results from the run's **Artifacts**, or from the `output/` folder,
   which the workflow commits back to the repository.

The workflow runs the tests first, then the scraper, uploads the files as
artifacts, and commits `output/listings.csv` and `output/listings.json`.

## Run locally (optional)

```bash
pip install -r requirements.txt
python -m pytest -q
TARGET_URL="https://yoursite.com/search?page={page}" python scraper.py
```

## Configuration

Every setting is an environment variable (or GitHub Actions variable). Defaults live in `config.py`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TARGET_URL` | placeholder (must be set) | Listing page URL; `{page}` enables numbered pagination |
| `NEXT_PAGE_SELECTOR` | empty | Follow a "next" link instead, e.g. `a[rel=next]@href` |
| `START_PAGE` / `PAGE_STEP` | `1` / `1` | First page number and increment |
| `MAX_PAGES` | `300` | Page limit |
| `MAX_LISTINGS` | `5000` | Stop after this many unique listings |
| `MIN_LISTINGS` | `1000` | Target; warns (or fails with `STRICT=true`) if not reached |
| `STALE_PAGE_LIMIT` | `2` | Stop after N pages with no new listings |
| `REQUEST_DELAY` | `1.0` | Seconds between page requests |
| `TIMEOUT` | `30` | Request timeout in seconds |
| `MAX_RETRIES` / `BACKOFF_FACTOR` | `4` / `2.0` | Retry attempts and exponential backoff |
| `RESPECT_ROBOTS` | `true` | Obey robots.txt |
| `USE_JSON_LD` | `true` | Fall back to schema.org JSON-LD data |
| `USER_AGENT` | built-in | Request User-Agent |
| `OUTPUT_DIR` / `OUTPUT_BASENAME` | `output` / `listings` | Output location |
| `STRICT` | `false` | Exit code 4 when fewer than `MIN_LISTINGS` are found |
| `LOG_LEVEL` | `INFO` | Logging level |

### CSS selectors

| Variable | Selects |
| --- | --- |
| `LISTING_SELECTOR` | One element per listing card |
| `ID_SELECTOR`, `TITLE_SELECTOR`, `URL_SELECTOR`, `PRICE_SELECTOR`, `ADDRESS_SELECTOR` | Fields inside a card |
| `PROPERTY_TYPE_SELECTOR`, `BEDROOMS_SELECTOR`, `BATHROOMS_SELECTOR`, `AREA_SELECTOR` | Property details |
| `DESCRIPTION_SELECTOR`, `IMAGE_SELECTOR` | Description and image |

Selector syntax:

- `.price` – text of the first match
- `a@href` – attribute of the first match
- `@data-id` – attribute of the card itself
- `img@data-src || img@src` – alternatives; the first non-empty value wins

To find selectors, open the listing page in a browser, right-click a listing,
choose **Inspect**, and note the classes of the card and its fields. If the site
embeds schema.org JSON-LD data, the scraper can use it automatically when no
cards match.

## Outputs

- `output/listings.csv` (UTF-8 with BOM, opens cleanly in Excel)
- `output/listings.json`

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | No listings collected (check URL and selectors) |
| 2 | Invalid configuration |
| 3 | Blocked by robots.txt |
| 4 | Fewer than `MIN_LISTINGS` collected with `STRICT=true` |

## Tests

```bash
python -m pytest -q
```

The tests run offline and cover parsing, JSON-LD, pagination, deduplication,
retries, robots.txt handling, configuration, and CSV/JSON export.

## Troubleshooting

- **"TARGET_URL is still the placeholder"** – set the `TARGET_URL` repository variable.
- **No listings found** – adjust `LISTING_SELECTOR` and the field selectors for your site.
- **Fewer than 1,000 listings** – the site may not have that many results for the URL;
  broaden the search filters or raise `MAX_PAGES`.
- **HTTP 403/429** – slow down with a higher `REQUEST_DELAY`. Some sites block
  cloud IP ranges or require a login or API.
- **JavaScript-rendered sites** – this scraper parses server-rendered HTML and JSON-LD;
  pages that load listings only via JavaScript need the site's underlying JSON API instead.

## Responsible use

Only scrape sites whose terms of service allow it, keep `RESPECT_ROBOTS=true`,
and keep a polite request delay. Handle any personal data in line with applicable
privacy laws.
