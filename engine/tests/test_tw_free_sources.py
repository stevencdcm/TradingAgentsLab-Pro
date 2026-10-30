"""S11 contract tests; all traffic is fixture backed."""
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

from engine.tw_free_sources import FreeTaiwanSources, HttpResponse
from engine.tw_source_catalog import get_source_catalog

FIXTURES = json.loads((Path(__file__).parent / "fixtures/tw_free_sources/responses.json").read_text())
AS_OF = "2026-10-30T12:00:00+08:00"


class FakeTime:
    def __init__(self):
        self.seconds = 0.0

    def clock(self):
        return self.seconds

    def sleep(self, duration):
        self.seconds += duration


class FreeSourceTests(unittest.TestCase):
    def client(self, responses, *, max_retries=1, min_interval_seconds=0):
        timer = FakeTime()
        calls = []

        def transport(url):
            calls.append(url)
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            if isinstance(response, HttpResponse):
                return response
            return HttpResponse(200, json.dumps(response).encode())

        client = FreeTaiwanSources(transport=transport, clock=timer.clock, sleep=timer.sleep,
                                   now=lambda: datetime(2026, 10, 30, tzinfo=timezone.utc),
                                   min_interval_seconds=min_interval_seconds, max_retries=max_retries)
        return client, timer, calls

    def test_financial_period_basis_and_unknown_publication(self):
        for dataset, stock, expected_basis, expected_unit in [
            ("income", "2330", "single_period", "per_share_TWD"),
            ("balance", "6488", "point_in_time", "TWD"),
            ("cash_flow", "2330", "year_to_date_cumulative", "TWD"),
        ]:
            client, _, calls = self.client([FIXTURES[dataset]])
            result = client.fetch_finmind_dataset(dataset, stock, "2025-01-01", as_of=AS_OF, end_date="2025-03-31")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.records, [])
            self.assertEqual(len(result.unknown_records), 1)
            row = result.unknown_records[0]
            self.assertEqual((row["data_period"], row["period_basis"], row["unit"]),
                             ("2025-03-31", expected_basis, expected_unit))
            self.assertEqual((row["published_at"], row["as_of"], row["currency"]), (None, AS_OF, "TWD"))
            self.assertEqual(row["consolidation"], "unknown")
            self.assertIn("published_at_unknown", result.gaps)
            self.assertIn("data_id=" + stock, calls[0])

    def test_news_is_one_day_and_never_claims_full_text(self):
        client, _, calls = self.client([FIXTURES["news"]])
        with self.assertRaises(ValueError):
            client.fetch_finmind_dataset("news", "6488", "2026-10-29", as_of=AS_OF, end_date="2026-10-29")
        result = client.fetch_finmind_dataset("news", "6488", "2026-10-29", as_of=AS_OF)
        self.assertNotIn("end_date", calls[0])
        self.assertEqual(result.records, [])
        self.assertEqual(result.unknown_records[0]["summary"], None)
        self.assertEqual(result.unknown_records[0]["article_url"], "https://example.org/article")
        self.assertEqual(result.unknown_records[0]["published_at"], None)

    def test_disclosures_do_not_turn_speech_or_event_date_into_publication(self):
        for market, stock, fixture in [("listed", "2330", "twse"), ("otc", "6488", "tpex")]:
            client, _, _ = self.client([FIXTURES[fixture]])
            result = client.fetch_disclosures(market, stock, as_of=AS_OF)
            self.assertEqual(result.records, [])
            row = result.unknown_records[0]
            self.assertEqual(row["data_period"], "2026-10-29")
            self.assertEqual(row["reported_speech_date"], "2026-10-29")
            self.assertIsNone(row["published_at"])
            self.assertIn("published_at_unknown", result.gaps)

    def test_future_period_is_excluded(self):
        client, _, _ = self.client([FIXTURES["income"]])
        result = client.fetch_finmind_dataset("income", "2330", "2025-01-01",
                                               as_of="2025-03-30T23:59:59+08:00")
        self.assertEqual(result.excluded_count, 1)
        self.assertEqual(result.unknown_records, [])

    def test_access_denied_never_retries(self):
        for status in (402, 403):
            client, _, calls = self.client([HttpResponse(status, b'{}')])
            result = client.fetch_finmind_dataset("income", "2330", "2025-01-01", as_of=AS_OF)
            self.assertEqual((result.status, result.http_status, len(calls)), ("access_denied", status, 1))

    def test_rate_limit_is_bounded_and_exposes_retry_after(self):
        client, timer, calls = self.client([HttpResponse(429, b'{}', {"Retry-After": "2"}),
                                           HttpResponse(429, b'{}', {"Retry-After": "3"})])
        result = client.fetch_finmind_dataset("news", "6488", "2026-10-29", as_of=AS_OF)
        self.assertEqual((result.status, result.retry_after, len(calls)), ("rate_limited", 3.0, 2))
        self.assertEqual(timer.seconds, 2.0)
        client, timer, calls = self.client([HttpResponse(429, b'{}', {"Retry-After": "3600"})])
        result = client.fetch_finmind_dataset("news", "6488", "2026-10-29", as_of=AS_OF)
        self.assertEqual((result.status, result.retry_after, len(calls), timer.seconds),
                         ("rate_limited", 3600.0, 1, 0.0))

    def test_expired_cache_is_not_served_on_failure(self):
        client, timer, calls = self.client([FIXTURES["income"], URLError("offline")], max_retries=0)
        first = client.fetch_finmind_dataset("income", "2330", "2025-01-01", as_of=AS_OF)
        second = client.fetch_finmind_dataset("income", "2330", "2025-01-01", as_of=AS_OF)
        self.assertTrue(second.cache_hit)
        self.assertEqual(len(calls), 1)
        timer.seconds = 3601
        third = client.fetch_finmind_dataset("income", "2330", "2025-01-01", as_of=AS_OF)
        self.assertEqual(third.status, "unavailable")
        self.assertTrue(third.cache_stale)
        self.assertEqual(third.unknown_records, [])
        self.assertEqual(first.fetched_at, second.fetched_at)

    def test_requires_aware_as_of_and_valid_stock(self):
        client, _, calls = self.client([])
        with self.assertRaises(ValueError):
            client.fetch_finmind_dataset("income", "2330", "2025-01-01", as_of="2025-01-01")
        with self.assertRaises(ValueError):
            client.fetch_disclosures("listed", "2330.evil", as_of=AS_OF)
        self.assertEqual(calls, [])

    def test_catalog_has_required_fields_and_does_not_claim_live_health(self):
        catalog = get_source_catalog()
        self.assertEqual([entry["id"] for entry in catalog], ["finmind_free", "twse_material_disclosures", "tpex_material_disclosures", "mops_historical_disclosures"])
        for entry in catalog:
            self.assertTrue({"name", "official_docs", "data_types", "free_scope", "token_requirement", "limits", "update_cadence", "license_notes", "verified_at", "implementation_status"} <= entry.keys())
            self.assertNotIn("last_success_at", entry)
        self.assertEqual(catalog[-1]["implementation_status"], "unsupported")
        catalog[0]["name"] = "changed"
        self.assertNotEqual(get_source_catalog()[0]["name"], "changed")


if __name__ == "__main__":
    unittest.main()
