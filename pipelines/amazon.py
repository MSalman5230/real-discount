"""Amazon short-link resolution and Amazon-specific response mapping."""

import re
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .common import build_result, fetch_page, make_session, request
from .errors import PriceHistoryError

HOSTS = {"amazon.in", "www.amazon.in", "amzn.to", "amzn-to.co", "amzn.urlgeni.us"}


def canonical_amazon_url(url):
    match = re.search(r"https?://(?:www\.)?amazon\.in/[^\s<>\"']+", unquote(url), re.I)
    if match:
        parts = urlsplit(match.group())
        asin = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:/|$)", parts.path, re.I)
        if asin:
            return "https://www.amazon.in/dp/" + asin.group(1).upper()
    return None


def resolve(session, url):
    seen = set()
    for _ in range(10):
        canonical = canonical_amazon_url(url)
        if canonical:
            return canonical
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.hostname not in HOSTS or url in seen:
            raise PriceHistoryError("Invalid or looping Amazon product link.", "invalid_url")
        seen.add(url)
        response = request(session, "GET", url, allow_redirects=False)
        location = response.headers.get("Location")
        if 300 <= response.status_code < 400 and location:
            url = urljoin(url, location)
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        refresh = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
        target = re.search(r"url\s*=\s*(.+)", refresh.get("content", ""), re.I) if refresh else None
        if target:
            url = urljoin(url, target.group(1).strip(" \"'"))
            continue
        raise PriceHistoryError("Link did not resolve to an Amazon product.", "invalid_url")
    raise PriceHistoryError("Too many Amazon redirects.", "invalid_url")


def normalize(input_url, product_url, soup, data, page_url, search):
    stats = data.get("Price") if isinstance(data.get("Price"), dict) else {}
    return build_result(input_url, product_url, product_url.rsplit("/", 1)[-1], "amazon",
                        soup, data, page_url, search, stats.get("Price"))


def fetch(input_url):
    with make_session() as session:
        url = resolve(session, input_url)
        return normalize(input_url, url, *fetch_page(session, url))
