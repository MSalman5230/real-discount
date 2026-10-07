"""Shared HTTP transport and validation; store-specific adapters live separately."""

import math
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import PriceHistoryError

BASE_URL = "https://pricehistory.app"
TIMEOUT = 25


def make_session():
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/129.0.0.0 Safari/537.36"
    retry = Retry(total=2, backoff_factor=.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"], respect_retry_after_header=False)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def request(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=TIMEOUT, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {404, 410}:
            raise PriceHistoryError("Product link or history page was not found.", "not_found") from None
        raise PriceHistoryError(f"HTTP request failed: {exc}", "network_error", True) from None


def json_object(response):
    try:
        value = response.json()
    except ValueError:
        raise PriceHistoryError("PriceHistory.app returned a non-JSON response.", "invalid_response") from None
    if not isinstance(value, dict):
        raise PriceHistoryError("PriceHistory.app returned an unexpected response shape.", "invalid_response")
    return value


def fetch_page(session, product_url):
    search = json_object(request(session, "POST", BASE_URL + "/api/search",
                                 json={"url": product_url}, headers={"Referer": BASE_URL + "/"}))
    if not search.get("status") or not search.get("code"):
        raise PriceHistoryError(search.get("message") or "No product found on PriceHistory.app.", "product_not_found")
    slug = str(search["code"])
    if not re.fullmatch(r"[\w-]+", slug, re.ASCII):
        raise PriceHistoryError("Invalid product-page identifier returned by the site.", "invalid_response")
    page = request(session, "GET", BASE_URL + "/p/" + slug)
    soup = BeautifulSoup(page.text, "html.parser")
    scripts = "\n".join(script.get_text() for script in soup.find_all("script"))
    code, token = page_header(scripts, "page"), page_header(scripts, "token")
    data = json_object(request(session, "POST", BASE_URL + "/api/price/" + code,
                               headers={"Content-Type": "application/json", "Origin": BASE_URL,
                                        "Referer": page.url, "page": code, "token": token}))
    return soup, data, page.url, search


def normalize_points(points):
    if not isinstance(points, list):
        return []
    valid = []
    for point in points:
        if not isinstance(point, dict) or not point.get("x") or point.get("y") is None:
            continue
        try:
            date = datetime.fromisoformat(str(point["x"]))
            price = float(point["y"])
            if not math.isfinite(price) or price <= 0:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        valid.append({**point, "x": date.isoformat(), "y": price})
    return sorted(valid, key=lambda point: point["x"])


def build_result(input_url, product_url, product_id, store, soup, data, page_url, search, current):
    history = data.get("History")
    if not isinstance(history, dict):
        raise PriceHistoryError("No historical prices are available for this product.", "no_history")
    prices = normalize_points(history.get("Price"))
    if not prices:
        raise PriceHistoryError("No usable historical prices are available for this product.", "no_history")
    try:
        current = float(current)
        if not math.isfinite(current) or current <= 0:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise PriceHistoryError("The site's current product price is unavailable.", "price_unavailable") from None
    # Verify the returned variant when the site exposes its store product code.
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) >= 2 and cells[0].get_text(" ", strip=True).lower() == "store product code":
            if cells[1].get_text(" ", strip=True).upper() != product_id.upper():
                raise PriceHistoryError("The site returned a different product variant.", "product_mismatch")
    stats = data.get("Price") if isinstance(data.get("Price"), dict) else {}
    heading = soup.find("h1")
    return {
        "input_url": input_url, "product_url": product_url, "product_id": product_id,
        "store": store, "currency": "INR", "history_url": page_url,
        "product_name": heading.get_text(" ", strip=True) if heading else search.get("name", product_id),
        "pricehistory_assessment": extract_assessment(soup),
        "scope": "All records collected by PriceHistory.app.",
        "summary": {
            "first_record": prices[0]["x"], "last_record": prices[-1]["x"],
            "price_observations": len(prices), "latest_price": current,
            "latest_price_updated_at": stats.get("UpdatedOn"),
            "lowest_price": stats.get("MinPrice", min(p["y"] for p in prices)),
            "lowest_price_at": stats.get("MinPriceOn"),
            "highest_price": stats.get("MaxPrice", max(p["y"] for p in prices)),
            "highest_price_at": stats.get("MaxPriceOn"),
            "lowest_offer_price": stats.get("MinOfferPrice"),
            "lowest_offer_price_at": stats.get("MinOfferPriceOn"),
        },
        "history": {"Price": prices, "OfferPrice": normalize_points(history.get("OfferPrice"))},
        "source_statistics": stats,
    }


def page_header(scripts, name):
    pattern = (
        r"FetchHeaders\s*\.\s*append\(\s*[\"']"
        + re.escape(name)
        + r"[\"']\s*,\s*[\"']([^\"']+)[\"']\s*\)"
    )
    match = re.search(pattern, scripts)
    if not match:
        raise PriceHistoryError(
            f"Cannot find the public page's {name} header; the site may have changed."
        )
    return match.group(1)

def extract_assessment(soup):
    """Read the site's selected buying label, not the four possible labels."""
    block = soup.select_one("#pricing-assessment")
    if block is None:
        return {"label": None, "explanation": None}
    selected = block.select_one(".scale-label.active")
    explanation = block.select_one(".supporting-text")
    text = explanation.get_text(" ", strip=True) if explanation else None
    marker = block.select_one(".rating-value")
    position = re.search(r"margin-left\s*:\s*([\d.]+)%", marker.get("style", "")) if marker else None
    increase = re.search(r"([\d.]+)%\s+chance.*?increase", text or "", re.I)
    prediction = re.search(r"([\d.]+)%\s+chance.*?\bwill\s+(increase|decrease|remain\s+constant)",
                           text or "", re.I)
    fluctuation = re.search(r"fluctuate\s+around\s+([\d.]+)%", text or "", re.I)
    return {
        "label": selected.get_text(" ", strip=True) if selected else None,
        "explanation": text,
        "scale_position_percent": float(position.group(1)) if position else None,
        "chance_of_increase_percent": float(increase.group(1)) if increase else None,
        "possible_fluctuation_percent": float(fluctuation.group(1)) if fluctuation else None,
        "prediction_probability_percent": float(prediction.group(1)) if prediction else None,
        "prediction_direction": " ".join(prediction.group(2).lower().split()) if prediction else None,
    }
