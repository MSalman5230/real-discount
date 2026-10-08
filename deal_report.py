"""Generate a six-month step chart and a Telegram-ready deal assessment."""

import argparse
import calendar
import json
import os
import textwrap
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from price_history import PriceHistoryError, fetch_all_history

IST = timezone(timedelta(hours=5, minutes=30))


def exceeds_threshold(current, median, threshold):
    """Strict comparison, without rounding a 20% boundary into an alert."""
    return Decimal(str(current)) < Decimal(str(median)) * (1 - Decimal(str(threshold)) / 100)


def subtract_months(date, months):
    index = date.year * 12 + date.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date.replace(year=year, month=month,
                        day=min(date.day, calendar.monthrange(year, month)[1]))


def local_datetime(value):
    date = datetime.fromisoformat(value)
    return date.astimezone(IST).replace(tzinfo=None) if date.tzinfo else date


def analyse_history(result, months=6, threshold=20, as_of=None):
    if months <= 0 or not 0 <= threshold <= 100:
        raise ValueError("Months must be positive and threshold must be between 0 and 100.")
    end = local_datetime(as_of) if isinstance(as_of, str) else as_of
    end = end or datetime.now(IST).replace(tzinfo=None)
    if end.tzinfo:
        end = end.astimezone(IST).replace(tzinfo=None)
    start = subtract_months(end, months)
    # A later observation at an identical timestamp replaces the earlier one.
    points = {}
    for point in result["history"]["Price"]:
        if point.get("x") and point.get("y") is not None:
            date = local_datetime(point["x"])
            if date <= end:
                points[date] = float(point["y"])
    points = sorted(points.items())
    if not points:
        raise PriceHistoryError("No prices were recorded before the report date.")
    current = float(result["summary"]["latest_price"])
    intervals = []
    for index, (date, price) in enumerate(points):
        next_date = points[index + 1][0] if index + 1 < len(points) else end
        left, right = max(date, start), min(next_date, end)
        if right > left:
            intervals.append((left, right, price))
    if not intervals:
        raise PriceHistoryError("No elapsed price-history interval is available yet.")
    durations = [(price, (right - left).total_seconds()) for left, right, price in intervals]
    halfway = sum(seconds for _, seconds in durations) / 2
    cumulative = 0
    for price, seconds in sorted(durations):
        cumulative += seconds
        if cumulative >= halfway:
            median = price
            break
    drop = 100 * (1 - current / median)
    report = {
        "months": months,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "carry_forward": True,
        "current_price": current,
        "time_weighted_median": median,
        "drop_percent": drop,
        "discount_threshold_percent": threshold,
        "target_price": median * (1 - threshold / 100),
        "median_rule_verdict": "BUY" if exceeds_threshold(current, median, threshold) else "WAIT",
        "website_verdict": result.get("pricehistory_assessment", {}).get("label"),
        "observed_points_in_window": sum(start <= date <= end for date, _ in points),
        "intervals": [{"start": left.isoformat(), "end": right.isoformat(), "price": price}
                      for left, right, price in intervals],
    }
    result["six_month_analysis"] = report
    return report


def report_caption(result):
    analysis = result["six_month_analysis"]
    site = result.get("pricehistory_assessment", {})
    name = result["product_name"]
    if len(name) > 180:
        name = name[:177] + "..."
    lines = [
        name,
        "",
        f"Latest tracked price: INR {analysis['current_price']:,.2f}",
        f"{analysis['months']}-month time-weighted median: INR {analysis['time_weighted_median']:,.2f}",
        f"Drop vs usual price: {analysis['drop_percent']:.1f}%",
    ]
    if site.get("prediction_probability_percent") is not None:
        lines.append(f"Site's stated prediction: {site['prediction_probability_percent']:g}% chance price will {site['prediction_direction']}")
    elif site.get("chance_of_increase_percent") is not None:
        lines.append(f"Site's stated chance of increase: {site['chance_of_increase_percent']:g}%")
    lines += ["", result["history_url"], "", result["product_url"]]
    return "\n".join(lines)


def render_graph(result, output=None):
    """Render a PNG to a memory buffer, or explicitly export it to a path."""
    os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).parent / ".mpl-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    analysis = result["six_month_analysis"]
    intervals = analysis["intervals"]
    x = [local_datetime(item["start"]) for item in intervals]
    y = [item["price"] for item in intervals]
    x.append(local_datetime(intervals[-1]["end"]))
    y.append(y[-1])
    current = analysis["current_price"]
    median = analysis["time_weighted_median"]
    target = analysis["target_price"]
    start, end = map(local_datetime, [analysis["window_start"], analysis["window_end"]])
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})
    fig, ax = plt.subplots(figsize=(13, 8.4), dpi=150)
    fig.patch.set_facecolor("#f8fafc")
    ax.set_facecolor("white")
    fig.subplots_adjust(left=.09, right=.965, top=.70, bottom=.22)
    title = textwrap.fill(result["product_name"], width=76)
    fig.text(.09, .96, title, va="top", fontsize=15, weight="bold", color="#152238")
    fig.text(.09, .86, f"{analysis['months']}-month history  ·  {start:%d %b %Y} – {end:%d %b %Y}",
             color="#526075", fontsize=11)
    stats = [("LATEST TRACKED PRICE", f"₹{current:,.2f}", "#087f5b"),
             ("TIME-WEIGHTED MEDIAN", f"₹{median:,.2f}", "#b7791f"),
             ("DROP VS MEDIAN", f"{analysis['drop_percent']:.1f}%", "#152238")]
    for left, (label, value, color) in zip([.09, .39, .72], stats):
        fig.text(left, .81, label, color="#526075", fontsize=9)
        fig.text(left, .76, value, color=color, fontsize=23, weight="bold")
    ax.step(x, y, where="post", lw=2.2, color="#2474b5", label="Recorded selling price")
    ax.axhline(median, color="#c08c28", lw=1.8, ls="--", label=f"Time-weighted median  ₹{median:,.2f}")
    ax.axhline(target, color="#778499", lw=1.5, ls=":",
               label=f"{analysis['discount_threshold_percent']:g}% drop target  ₹{target:,.2f}")
    ax.scatter([end], [current], color="#087f5b", s=75, zorder=6, clip_on=False)
    ax.annotate(f"Latest ₹{current:,.2f}", (end, current), xytext=(-12, -25),
                textcoords="offset points", ha="right", fontsize=10, color="#087f5b",
                bbox={"facecolor":"white", "edgecolor":"none", "alpha":.9, "pad":3})
    low, high = min(y + [current, median, target]), max(y + [current, median, target])
    margin = max((high - low) * .12, median * .025)
    ax.set_ylim(max(0, low - margin), high + margin)
    ax.set_xlim(start, end)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"₹{value:,.0f}"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.grid(axis="y", color="#e6ebf1", lw=.8)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["bottom", "left"]].set_color("#d5dce5")
    ax.tick_params(colors="#526075", length=0, pad=9)
    ax.legend(loc="upper center", bbox_to_anchor=(.5, -.14), ncol=3,
              frameon=False, fontsize=9)
    site = result.get("pricehistory_assessment", {})
    fig.text(.09, .105, f"PriceHistory.app says: {site.get('label') or 'Unavailable'}",
             fontsize=13, weight="bold", color="#152238")
    fig.text(.53, .105, f"Your >{analysis['discount_threshold_percent']:g}% rule: {analysis['median_rule_verdict']}",
             fontsize=13, weight="bold", color="#152238")
    if site.get("prediction_probability_percent") is not None:
        fig.text(.09, .075, f"Site's stated prediction: {site['prediction_probability_percent']:g}% chance price will {site['prediction_direction']}",
                 fontsize=10, color="#526075")
    elif site.get("chance_of_increase_percent") is not None:
        fig.text(.09, .075, f"Site's stated chance of a price increase: {site['chance_of_increase_percent']:g}%",
                 fontsize=10, color="#526075")
    fig.text(.09, .035, "Source: PriceHistory.app  ·  Prices carried forward until the next observation  ·  Currency: INR",
             fontsize=9, color="#526075")
    if output is None:
        output = BytesIO()
    else:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(output, format="png", facecolor=fig.get_facecolor())
        if hasattr(output, "seek"):
            output.seek(0)
        return output
    except Exception:
        if isinstance(output, BytesIO):
            output.close()
        raise
    finally:
        plt.close(fig)


def prepare_report(url, months=6, threshold=20, as_of=None):
    result = fetch_all_history(url)
    analyse_history(result, months, threshold, as_of)
    return result


def save_report(result, output_dir=Path("results"), graph=True):
    slug = result.get("product_id") or result["product_url"].rstrip("/").split("/")[-1]
    slug = "".join(char if char.isalnum() else "-" for char in slug)[:60] or "product"
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    image = render_graph(result, directory / f"{slug}-6months.png") if graph else None
    (directory / f"{slug}-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    caption = report_caption(result)
    (directory / f"{slug}-message.txt").write_text(caption + "\n", encoding="utf-8")
    return result, image, caption


def create_report(url, output_dir=Path("results"), months=6, threshold=20, as_of=None):
    return save_report(prepare_report(url, months, threshold, as_of), output_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--months", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--as-of", help="Optional reproducible end date/time in IST")
    parser.add_argument("--output-dir", type=Path,
                        help="Explicitly save a graph, JSON and caption; otherwise keep processing in memory")
    parser.add_argument("--send", action="store_true", help="Send report to TELEGRAM_CHAT_ID")
    args = parser.parse_args()
    try:
        from settings import read_settings
        settings = read_settings()
        result = prepare_report(args.url,
                                args.months if args.months is not None else settings.months,
                                args.threshold if args.threshold is not None else settings.threshold,
                                args.as_of)
        caption = report_caption(result)
        if args.output_dir is not None:
            _, graph, _ = save_report(result, args.output_dir)
            print(f"Graph: {graph.resolve()}")
        print(caption.encode("ascii", "backslashreplace").decode())
        if args.send:
            from telegram_monitor import TelegramBot, load_config
            load_config()
            import os
            recipient = os.environ.get("TELEGRAM_CHAT_ID")
            if not recipient:
                raise ValueError("Set TELEGRAM_CHAT_ID after starting your bot; run telegram_monitor.py discover.")
            if result["six_month_analysis"]["median_rule_verdict"] != "BUY":
                print("No Telegram alert: drop does not exceed the configured threshold.")
                return
            with render_graph(result) as graph:
                TelegramBot().send_report(recipient, graph, caption)
            print("Report sent to Telegram.")
    except (ValueError, PriceHistoryError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
