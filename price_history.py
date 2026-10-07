"""Fetch Amazon or Flipkart history without JavaScript; CLI compatibility facade."""
import argparse
import csv
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
import requests
from pipelines import fetch_history as fetch_all_history
from pipelines.errors import PriceHistoryError
from pipelines.common import extract_assessment, page_header
from pipelines.amazon import canonical_amazon_url


def write_csv(path, history):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["series", "timestamp", "price", "offers"])
        for name, points in history.items():
            for point in points:
                if point.get("x") and point.get("y") is not None:
                    writer.writerow([
                        name, point["x"], point["y"],
                        "; ".join(point.get("Offers") or []),
                    ])

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Product URL or short link")
    parser.add_argument("--output", type=Path, help="Save full history as JSON")
    parser.add_argument("--csv", type=Path, help="Also export the recorded series as CSV")
    args = parser.parse_args()
    if urlsplit(args.url).scheme not in {"http", "https"}:
        parser.error("Provide an HTTP or HTTPS product link.")
    try:
        result = fetch_all_history(args.url)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps({key: value for key, value in result.items()
                              if key not in {"history", "source_statistics"}},
                             ensure_ascii=True, indent=2))
            print(f"Full history saved to {args.output}")
        else:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        if args.csv:
            write_csv(args.csv, result["history"])
            print(f"CSV saved to {args.csv}", file=sys.stderr)
    except (requests.RequestException, PriceHistoryError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
