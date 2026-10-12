import tempfile
import unittest
from pathlib import Path

from report import ReportValidationError, format_report, report_path, save_report


VALID_REPORT = """## AI Daily News Report
**Report Date:** 2026-10-12
**Research Window:** start to end
**Generated At:** timestamp
### Executive Summary
One verified development.
### Top AI News
#### Example
Summary and why it matters.
Publication date: unavailable/unverified.
Source: https://example.com/news
### Emerging Trends
Evidence remains limited.
### Sources
- https://example.com/news
"""


def evidence(title, url, snippet, publication_date=""):
    return {"title": title, "url": url, "snippet": snippet, "publication_date": publication_date}


def evidence_report(items):
    body = "\n".join(
        f"#### {item['headline']}\nSummary: {item['summary']}\nPublication date: {item['date']}\nSources: {item['sources']}"
        for item in items
    )
    sources = "\n".join(f"- {url}" for item in items for url in item["sources"].split(", "))
    return f"""## AI Daily News Report
**Report Date:** 2026-10-12
**Research Window:** 2026-10-11 through 2026-10-12
**Generated At:** 2026-10-12
### Executive Summary
Verified items follow.
### Top AI News
{body}
### Emerging Trends
No broader trend asserted.
### Sources
{sources}
"""


class ReportTests(unittest.TestCase):
    def test_format_valid_report(self):
        self.assertTrue(format_report(VALID_REPORT).endswith("\n"))

    def test_rejects_missing_structure(self):
        with self.assertRaises(ReportValidationError):
            format_report("short")

    def test_no_results_report_is_valid_without_url(self):
        no_results = """## AI Daily News Report
**Report Date:** 2026-10-12
**Research Window:** start to end
**Generated At:** timestamp
### Executive Summary
No relevant recent news found in this research window.
### Top AI News
No relevant recent news found.
### Emerging Trends
No supported trend.
### Sources
No sources were used because no relevant results were found.
"""
        self.assertIn("No relevant recent news", format_report(no_results))

    def test_save_and_generate_versioned_filename(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = save_report(VALID_REPORT, root, "2026-10-12")
            second = save_report(VALID_REPORT, root, "2026-10-12")
            self.assertEqual(first.name, "ai_news_2026-10-12.md")
            self.assertEqual(second.name, "ai_news_2026-10-12_2.md")
            self.assertEqual(first.read_text(encoding="utf-8"), format_report(VALID_REPORT))

    def test_filename_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(report_path(Path(temp), "2026-10-12").name, "ai_news_2026-10-12.md")

    def test_failed_validation_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ReportValidationError):
                save_report("bad", Path(temp), "2026-10-12")
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_rejects_exact_url_duplicate_across_items(self):
        url = "https://example.com/story"
        items = [
            {"headline": "Nimbus releases Atlas", "summary": "Nimbus released Atlas today.", "date": "Not verified", "sources": url},
            {"headline": "Nimbus Atlas launch", "summary": "Nimbus released Atlas for users.", "date": "Not verified", "sources": url},
        ]
        with self.assertRaisesRegex(ReportValidationError, "repeats a source URL"):
            format_report(evidence_report(items), [evidence("Nimbus releases Atlas", url, "Nimbus released Atlas today.")])

    def test_rejects_same_story_under_different_urls(self):
        first_url = "https://news.example.com/nimbus-atlas"
        second_url = "https://blog.example.org/atlas-launch"
        items = [
            {"headline": "Nimbus releases Atlas research model", "summary": "Nimbus released Atlas research model.", "date": "Not verified", "sources": first_url},
            {"headline": "Nimbus announces new Atlas research model", "summary": "Nimbus released its Atlas research model.", "date": "Not verified", "sources": second_url},
        ]
        sources = [
            evidence("Nimbus releases Atlas research model", first_url, "Nimbus released Atlas research model."),
            evidence("Nimbus announces new Atlas research model", second_url, "Nimbus released its Atlas research model."),
        ]
        with self.assertRaisesRegex(ReportValidationError, "duplicate stories"):
            format_report(evidence_report(items), sources)

    def test_distinct_stories_from_same_organization_are_allowed(self):
        one = "https://example.com/nimbus-atlas"
        two = "https://example.com/nimbus-funding"
        items = [
            {"headline": "Nimbus releases Atlas model", "summary": "Nimbus released Atlas model.", "date": "Not verified", "sources": one},
            {"headline": "Nimbus raises funding round", "summary": "Nimbus raised a funding round.", "date": "Not verified", "sources": two},
        ]
        sources = [
            evidence("Nimbus releases Atlas model", one, "Nimbus released Atlas model."),
            evidence("Nimbus raises funding round", two, "Nimbus raised a funding round."),
        ]
        self.assertIn("Nimbus raises funding round", format_report(evidence_report(items), sources))

    def test_rejects_unsupported_benchmark_description(self):
        url = "https://example.com/android-bench"
        item = {"headline": "Android Bench 2.0", "summary": "Android Bench 2.0 measures on-device AI inference workloads.", "date": "Not verified", "sources": url}
        source = evidence("Android Bench 2.0 evaluates Android development tasks", url, "Google introduced Android Bench 2.0 for long-horizon Android development tasks and agent evaluation.")
        with self.assertRaisesRegex(ReportValidationError, "technical claim"):
            format_report(evidence_report([item]), [source])

    def test_missing_source_date_requires_not_verified(self):
        url = "https://example.com/story"
        item = {"headline": "Nimbus updates Atlas", "summary": "Nimbus updated Atlas.", "date": "October 12, 2026", "sources": url}
        with self.assertRaisesRegex(ReportValidationError, "must be marked Not verified"):
            format_report(evidence_report([item]), [evidence("Nimbus updates Atlas", url, "Nimbus updated Atlas.")])

    def test_rejects_invalid_source_url(self):
        item = {"headline": "Nimbus updates Atlas", "summary": "Nimbus updated Atlas.", "date": "Not verified", "sources": "http:///missing-host"}
        with self.assertRaisesRegex(ReportValidationError, "invalid source URL"):
            format_report(evidence_report([item]), [evidence("Nimbus updates Atlas", "https://example.com/story", "Nimbus updated Atlas.")])

    def test_rejects_item_without_retrieved_evidence(self):
        url = "https://not-retrieved.example/story"
        item = {"headline": "Nimbus updates Atlas", "summary": "Nimbus updated Atlas.", "date": "Not verified", "sources": url}
        with self.assertRaisesRegex(ReportValidationError, "not returned by search"):
            format_report(evidence_report([item]), [])

    def test_rejects_summary_with_insufficient_evidence_overlap(self):
        url = "https://example.com/story"
        item = {"headline": "Nimbus releases Atlas", "summary": "Nimbus released Atlas.", "date": "Not verified", "sources": url}
        source = evidence("Garden flowers", url, "Unrelated content about gardening and spring flowers.")
        with self.assertRaisesRegex(ReportValidationError, "not sufficiently supported"):
            format_report(evidence_report([item]), [source])

    def test_preserves_and_checks_a_reliably_supplied_publication_date(self):
        url = "https://example.com/dated-story"
        source = evidence("Nimbus releases Atlas", url, "Nimbus released Atlas research model.", "Tue, 11 Mar 2025 17:00:00 GMT")
        valid_item = {"headline": "Nimbus releases Atlas", "summary": "Nimbus released Atlas research model.", "date": "11 Mar 2025", "sources": url}
        self.assertIn("Publication date: 11 Mar 2025", format_report(evidence_report([valid_item]), [source]))
        invalid_item = dict(valid_item, date="12 Mar 2025")
        with self.assertRaisesRegex(ReportValidationError, "does not match"):
            format_report(evidence_report([invalid_item]), [source])

    def test_accepts_multiple_supporting_urls_for_one_story(self):
        first_url = "https://news.example.com/nimbus-atlas"
        second_url = "https://nimbus.example.com/atlas-announcement"
        item = {"headline": "Nimbus releases Atlas", "summary": "Nimbus released Atlas model.", "date": "Not verified", "sources": f"{first_url}, {second_url}"}
        sources = [
            evidence("Nimbus releases Atlas", first_url, "Nimbus released Atlas model."),
            evidence("Nimbus releases Atlas", second_url, "Nimbus released Atlas model."),
        ]
        self.assertIn("Nimbus releases Atlas", format_report(evidence_report([item]), sources))

    def test_allows_fewer_stories_than_target_when_supported(self):
        url = "https://example.com/one-story"
        item = {"headline": "Nimbus releases Atlas", "summary": "Nimbus released Atlas.", "date": "Not verified", "sources": url}
        report = evidence_report([item])
        self.assertEqual(format_report(report, [evidence("Nimbus releases Atlas", url, "Nimbus released Atlas.")]), report)


if __name__ == "__main__":
    unittest.main()
