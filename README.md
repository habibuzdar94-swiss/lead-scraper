# Realtor.com property listing scraper

This project reads search-page URLs from `urls.txt`, filters out articles, policy pages, and individual property-detail URLs, then scrapes search-result pages with pagination. It exports `output/listings.csv` and `output/listings.json`.

## Run in GitHub Actions

1. Replace the files in your repository with the files in this folder, keeping `.github/workflows/scrape.yml` at that exact path.
2. Commit and push. The workflow also runs weekly, or use Actions → **Scrape Realtor.com property listings** → **Run workflow**.
3. Open the workflow run and download the `realtor-listings-*` artifact. If GitHub is allowed to commit output, the CSV/JSON will also be in `output/`.

## Settings

GitHub repository → Settings → Secrets and variables → Actions → Variables:

- `MAX_SEEDS`: number of unique location searches to process (default `60`; `urls.txt` currently contains many more).
- `MAX_PAGES_PER_SEED`: pagination limit per search (default `5`).
- `MAX_TOTAL_PAGES`: total pages across all searches (default `200`).
- `MAX_LISTINGS`: maximum unique properties (default `5000`).
- `REQUEST_DELAY`: seconds between page requests; minimum is 1 (default `2`).

To cover all locations in the supplied URL collection, increase `MAX_SEEDS` to the number of lines in `urls.txt`; also increase `MAX_TOTAL_PAGES` gradually. A very large crawl can exceed GitHub Actions' time limit or trigger site rate limits, so start with defaults.

## Important limitations

- `urls.txt` was filtered from the supplied URL collection to location-level Realtor.com search seeds. The original list had articles, account pages, detail pages, filter variants, and duplicates; those are not treated as independent searches.
- The scraper first parses embedded `__NEXT_DATA__`, then tries known HTML card selectors. Realtor.com can change its markup or return HTTP 403/429 to automated traffic. The script stops on 403/429 instead of trying to bypass the block. If it returns zero records, inspect the Actions log; use an authorized data provider/API if the site blocks automated access.
- The code does not guarantee 1,000 records: that depends on which search pages are accessible and the listings available in them.
- Use the data in compliance with Realtor.com's terms, robots.txt, and applicable law. The default request rate is deliberately low.
