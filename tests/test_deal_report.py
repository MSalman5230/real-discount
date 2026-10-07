import unittest
from datetime import datetime

from bs4 import BeautifulSoup

from deal_report import analyse_history, subtract_months
from price_history import extract_assessment
from telegram_monitor import AUTHORIZED_SOURCE, AUTHORIZED_GROUP_NAME, authorized_source, verify_group, extract_links


def product(points, current):
    return {"history": {"Price": [{"x": date, "y": price} for date, price in points]},
            "summary": {"latest_price": current}}


class DealAnalysisTests(unittest.TestCase):
    def test_short_price_spike_does_not_change_usual_price(self):
        result = product([("2026-04-01", 1000), ("2026-06-29", 5000),
                          ("2026-06-30", 800)], 800)
        analysis = analyse_history(result, as_of="2026-07-01")
        self.assertEqual(analysis["time_weighted_median"], 1000)
        self.assertAlmostEqual(analysis["drop_percent"], 20)
        self.assertEqual(analysis["median_rule_verdict"], "WAIT")

    def test_price_before_cutoff_and_last_price_are_carried_forward(self):
        result = product([("2025-01-01", 100), ("2026-05-01", 80)], 80)
        analysis = analyse_history(result, as_of="2026-07-01")
        self.assertEqual(analysis["intervals"][0]["start"], "2026-01-01T00:00:00")
        self.assertEqual(analysis["intervals"][-1]["end"], "2026-07-01T00:00:00")
        self.assertEqual(analysis["time_weighted_median"], 100)

    def test_month_subtraction_uses_calendar_months(self):
        self.assertEqual(subtract_months(datetime(2026, 8, 31), 6), datetime(2026, 2, 28))

    def test_duplicate_timestamp_uses_latest_observation(self):
        result = product([("2026-01-01", 100), ("2026-01-01", 200)], 200)
        self.assertEqual(analyse_history(result, as_of="2026-07-01")["time_weighted_median"], 200)


class ExtractionTests(unittest.TestCase):
    def test_only_selected_site_assessment_is_returned(self):
        soup = BeautifulSoup('''<div id="pricing-assessment">
          <div class="scale-label">Skip</div><div class="scale-label active">Wait</div>
          <div class="scale-label">Okay</div><div class="scale-label">Yes</div>
          <div class="rating-value" style="margin-left: 30%;"></div>
          <p class="supporting-text">There is 51.00% chance that the price will increase.
          Price might fluctuate around 3% from current price.</p></div>''', "html.parser")
        assessment = extract_assessment(soup)
        self.assertEqual(assessment["label"], "Wait")
        self.assertEqual(assessment["chance_of_increase_percent"], 51)
        self.assertEqual(assessment["possible_fluctuation_percent"], 3)

    def test_missing_assessment_is_not_guessed(self):
        self.assertIsNone(extract_assessment(BeautifulSoup("<div></div>", "html.parser"))["label"])

    def test_site_constant_price_prediction_is_preserved(self):
        soup = BeautifulSoup('''<div id="pricing-assessment"><div class="scale-label active">Yes</div>
          <p class="supporting-text">There is 39.01% chance that the price of Core Bags will remain constant.</p>
          </div>''', "html.parser")
        assessment = extract_assessment(soup)
        self.assertEqual(assessment["prediction_probability_percent"], 39.01)
        self.assertEqual(assessment["prediction_direction"], "remain constant")

    def test_group_links_include_hidden_and_button_links(self):
        links = extract_links("Deal (https://amzn-to.co/xnXqN2). https://t.me/unrelated",
                              [{"type": "text_link", "url": "https://www.amazon.in/dp/B0H9SB84R1"}],
                              ["https://amzn.to/another"])
        self.assertEqual(links, ["https://amzn-to.co/xnXqN2", "https://www.amazon.in/dp/B0H9SB84R1",
                                 "https://amzn.to/another"])


class GroupScopeTests(unittest.TestCase):
    def test_only_authorized_source_is_accepted(self):
        self.assertEqual(authorized_source(AUTHORIZED_SOURCE), AUTHORIZED_SOURCE)
        for value in ["", "https://t.me/another", AUTHORIZED_SOURCE + ",https://t.me/another"]:
            with self.assertRaises(ValueError):
                authorized_source(value)

    def test_wrong_group_name_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_group("Another Deals Group", -100123)

    def test_a_name_collision_cannot_change_the_pinned_group(self):
        binding = verify_group(AUTHORIZED_GROUP_NAME, -100123)
        with self.assertRaises(ValueError):
            verify_group(AUTHORIZED_GROUP_NAME, -100456, binding)
        self.assertEqual(verify_group(AUTHORIZED_GROUP_NAME, -100123, binding), binding)


if __name__ == "__main__":
    unittest.main()
