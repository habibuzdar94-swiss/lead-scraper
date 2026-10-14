import json
from scraper import Listing, make_page_url, parse_html, search_seed, read_seeds


def test_search_seed_filters_non_search_urls():
    assert search_seed('https://www.realtor.com/realestateandhomes-search/Baltimore_MD/pg-2') == 'https://www.realtor.com/realestateandhomes-search/Baltimore_MD'
    assert search_seed('https://www.realtor.com/realestateandhomes-detail/abc') is None
    assert search_seed('https://www.realtor.com/news/') is None


def test_page_url():
    seed = 'https://www.realtor.com/realestateandhomes-search/Baltimore_MD'
    assert make_page_url(seed, 1) == seed
    assert make_page_url(seed, 2).endswith('/pg-2')


def test_parse_next_data_payload():
    payload = {
      "props": {"pageProps": {"searchResults": {"home_search": {"results": [{
        "property_id": "123456", "permalink": "15993-E-Loyola-Dr_Aurora_CO_80013_M24196-27621",
        "list_price": 179900, "location": {"address": {"line": "15993 E Loyola Dr", "city": "Aurora", "state_code": "CO", "postal_code": "80013"}},
        "description": {"beds": 2, "baths_consolidated": "1", "sqft": 320}, "type": "condo"
      }]}}}}
    }
    html = '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script></html>'
    rows = parse_html(html, 'https://www.realtor.com/realestateandhomes-search/Aurora_CO')
    assert len(rows) == 1
    assert rows[0].listing_id == '123456'
    assert rows[0].price == '$179,900'
    assert rows[0].address == '15993 E Loyola Dr, Aurora, CO, 80013'
    assert rows[0].bedrooms == '2'
    assert rows[0].url.startswith('https://www.realtor.com/')


def test_dom_fallback():
    html = '''<div data-testid="property-card"><a data-testid="card-anchor" href="/realestateandhomes-detail/a_M12345-12345"></a><span data-testid="card-price">$250,000</span><div data-testid="card-address">12 Main St</div><span data-testid="property-meta-beds">3 beds</span></div>'''
    rows = parse_html(html, 'https://www.realtor.com/realestateandhomes-search/Test_CA')
    assert len(rows) == 1
    assert rows[0].price == '$250,000'
    assert rows[0].address == '12 Main St'
