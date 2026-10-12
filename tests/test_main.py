import tempfile
import unittest
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config import Settings
from main import execute_run
from agent import ResearchResult
from report import StoryCandidate
from report import ReportValidationError


class MainTests(unittest.TestCase):
    def make_settings(self, root):
        return Settings("groq-test-secret-value", "tavily-test-secret-value", "model", "Asia/Karachi", root / "reports", root / "logs", 4, 5, 30, 24, "INFO")

    def test_mocked_workflow_saves_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixed_now = datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Karachi"))
            evidence = [{"title": "Example AI update", "url": "https://example.com/news", "snippet": "An example AI update was announced.", "publication_date": ""}]
            result = ResearchResult(
                search_calls=1,
                stories=[StoryCandidate(
                    headline="Example AI update",
                    summary="An example AI update was announced.",
                    publication_date=None,
                    source_urls=["https://example.com/news"],
                    claims=[{"claim_text": "An example AI update was announced.", "source_url": "https://example.com/news", "evidence_excerpt": "An example AI update was announced."}],
                    interpretation="It may be relevant to developers.",
                )],
                evidence=evidence,
            )
            with patch("main.run_research", return_value=result):
                with self.assertLogs("main", level="INFO") as captured:
                    path = execute_run(self.make_settings(root), now=fixed_now)
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "ai_news_2026-10-12.md")
            report_text = path.read_text(encoding="utf-8")
            self.assertIn("#### Example AI update", report_text)
            self.assertIn("Why it matters (interpretation): It may be relevant to developers.", report_text)
            self.assertNotIn("An example AI update was announced.", "\n".join(captured.output))
            self.assertEqual(list(root.joinpath("reports").glob("*.audit.json")), [])

    def test_failed_research_does_not_save_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.make_settings(root)
            with patch("main.run_research", side_effect=RuntimeError("mock failure")):
                with self.assertRaises(RuntimeError):
                    execute_run(settings)
            self.assertFalse(settings.report_dir.exists())

    def test_enabled_audit_is_saved_alongside_unchanged_markdown_without_extra_provider_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixed_now = datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Karachi"))
            url = "https://example.com/news"
            snippet = "An example AI update was announced."
            evidence = [{"title": "Example AI update", "url": url, "snippet": snippet, "publication_date": ""}]
            story = StoryCandidate(
                headline="Example AI update", summary="Original summary is discarded.", source_urls=[url],
                claims=[{"claim_text": snippet, "source_url": url, "evidence_excerpt": snippet}],
            )
            result = ResearchResult(stories=[story], evidence=evidence, search_calls=1, model_calls=1)
            settings = replace(self.make_settings(root), evidence_audit_enabled=True)
            with patch("main.run_research", return_value=result) as run_research:
                report = execute_run(settings, now=fixed_now)
            run_research.assert_called_once()
            audit_path = report.with_name(report.stem + ".audit.json")
            self.assertTrue(audit_path.exists())
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            claim_record = payload["accepted_stories"][0]["claims"][0]
            self.assertEqual(claim_record["source_url"], url)
            self.assertEqual(claim_record["evidence_excerpt"], snippet)
            self.assertEqual(claim_record["source_snippet"], snippet)
            self.assertEqual(claim_record["validation_status"], "accepted")
            self.assertIn("Summary: An example AI update was announced.", report.read_text(encoding="utf-8"))
            self.assertNotIn("Original summary is discarded", report.read_text(encoding="utf-8"))

    def test_enabled_audit_is_not_created_when_validation_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = replace(self.make_settings(root), evidence_audit_enabled=True)
            fixed_now = datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Karachi"))
            result = ResearchResult(
                stories=[StoryCandidate(headline=None, summary="No headline.", source_urls=[])], evidence=[],
            )
            with patch("main.run_research", return_value=result):
                with self.assertRaises(ReportValidationError):
                    execute_run(settings, now=fixed_now)
            self.assertFalse(settings.report_dir.exists())

    def test_no_valid_candidates_does_not_save_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.make_settings(root)
            fixed_now = datetime(2026, 10, 12, 9, tzinfo=ZoneInfo("Asia/Karachi"))
            result = ResearchResult(
                stories=[StoryCandidate(headline=None, summary="No headline.", source_urls=[])],
                evidence=[],
            )
            with patch("main.run_research", return_value=result):
                with self.assertRaisesRegex(ReportValidationError, "No story candidates"):
                    execute_run(settings, now=fixed_now)
            self.assertFalse(settings.report_dir.exists())


if __name__ == "__main__":
    unittest.main()
