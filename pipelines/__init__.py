"""Route product links to independent store adapters."""

from urllib.parse import urlsplit

from . import amazon, flipkart
from .errors import PriceHistoryError


def fetch_history(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except (TypeError, ValueError):
        raise PriceHistoryError("Invalid product URL.", "invalid_url") from None
    if parts.scheme not in {"https", "http"} or parts.username or parts.password:
        raise PriceHistoryError("Provide a valid HTTP(S) product URL.", "invalid_url")
    if host in amazon.HOSTS:
        return amazon.fetch(url)
    if host in flipkart.HOSTS:
        return flipkart.fetch(url)
    raise PriceHistoryError("This link is not a supported Amazon India or Flipkart product link.", "unsupported_store")
