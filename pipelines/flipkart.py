"""Flipkart affiliate links, PID preservation, and Flipkart response mapping."""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .common import build_result, fetch_page, make_session, request
from .errors import PriceHistoryError

HOSTS = {"fkrt.co", "www.fkrt.co", "flipkart.com", "www.flipkart.com", "dl.flipkart.com", "m.flipkart.com"}


def canonical_flipkart_url(url):
    parts = urlsplit(url)
    if parts.hostname not in {"flipkart.com", "www.flipkart.com", "dl.flipkart.com", "m.flipkart.com"}:
        return None
    path = parts.path[3:] if parts.path.startswith("/dl/") else parts.path
    pid = parse_qs(parts.query).get("pid", [None])[0]
    if not re.search(r"/p/itm[A-Za-z0-9]+", path) or not pid or not re.fullmatch(r"[A-Za-z0-9]{8,32}", pid):
        return None
    return "https://www.flipkart.com" + path.rstrip("/") + "?" + urlencode({"pid": pid.upper()})


def resolve(session, url):
    seen = set()
    for _ in range(10):
        canonical = canonical_flipkart_url(url)
        if canonical:
            return canonical
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or parts.hostname not in HOSTS or url in seen:
            raise PriceHistoryError("Invalid or looping Flipkart product link.", "invalid_url")
        seen.add(url)
        response = request(session, "GET", url, allow_redirects=False)
        location = response.headers.get("Location")
        if 300 <= response.status_code < 400 and location:
            url = urljoin(url, location)
            continue
        raise PriceHistoryError("Link did not resolve to a Flipkart product with a PID.", "invalid_url")
    raise PriceHistoryError("Too many Flipkart redirects.", "invalid_url")


def normalize(input_url, product_url, soup, data, page_url, search):
    stats = data.get("Price") if isinstance(data.get("Price"), dict) else {}
    main = data.get("Main") if isinstance(data.get("Main"), dict) else {}
    current = stats.get("Price") if stats.get("Price") is not None else main.get("Price")
    pid = parse_qs(urlsplit(product_url).query)["pid"][0]
    # A missing buying-assessment widget is normal on Flipkart pages; do not
    # infer an Amazon-style Yes/Wait label from prices or page layout.
    return build_result(input_url, product_url, pid, "flipkart", soup, data, page_url, search, current)


def fetch(input_url):
    with make_session() as session:
        url = resolve(session, input_url)
        return normalize(input_url, url, *fetch_page(session, url))
