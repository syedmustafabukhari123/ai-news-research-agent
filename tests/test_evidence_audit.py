import json
import tempfile
import unittest
from pathlib import Path

from report import (
    EvidenceAuditError,
    StoryValidationResult,
    ValidatedClaim,
    ValidatedStory,
    build_evidence_audit,
    save_evidence_audit,
)


class EvidenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.url = "https://example.com/story"
        self.source = {
            "title": "Nimbus announces Atlas",
            "url": self.url,
            "snippet": "Nimbus announced Atlas for developers.",
            "publication_date": "Tue, 11 Mar 2025 17:00:00 GMT",
        }
        self.claim = ValidatedClaim(
            "Nimbus announced Atlas for developers.", self.url, "Nimbus announced Atlas for developers."
        )
        self.story = ValidatedStory(
            "Nimbus announces Atlas", "Nimbus announced Atlas for developers.",
            self.source["publication_date"], [self.url], claims=[self.claim],
        )
        self.validation = StoryValidationResult(accepted=[self.story])
        self.candidates = [{"headline": self.story.headline, "claims": []}]

    def test_successful_atomic_audit_generation(self):
        payload = build_evidence_audit(
            self.candidates, self.validation, [self.source],
            report_file="ai_news_2026-10-12.md", generated_at="2026-10-12T09:00:00+05:00",
        )
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "ai_news_2026-10-12.md"
            audit_path = save_evidence_audit(payload, report_path)
            saved = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit_path.name, "ai_news_2026-10-12.audit.json")
            self.assertEqual(saved["accepted_stories"][0]["headline"], self.story.headline)
            self.assertEqual(saved["accepted_stories"][0]["claims"][0]["source_url"], self.url)
            self.assertEqual(saved["accepted_stories"][0]["claims"][0]["source_title"], self.source["title"])
            self.assertEqual(saved["accepted_stories"][0]["claims"][0]["source_snippet"], self.source["snippet"])
            self.assertEqual(saved["accepted_stories"][0]["claims"][0]["validation_status"], "accepted")
            self.assertEqual([item.name for item in Path(temp).iterdir()], [audit_path.name])

    def test_claim_is_associated_only_with_its_own_source(self):
        wrong_source = dict(self.source, url="https://example.com/other", title="Other story", snippet="Unrelated source text.")
        with self.assertRaises(EvidenceAuditError):
            build_evidence_audit(self.candidates, self.validation, [wrong_source], report_file="report.md", generated_at="now")

    def test_missing_or_malformed_evidence_fails_closed(self):
        for evidence in ([], [dict(self.source, snippet=None)]):
            with self.subTest(evidence=evidence):
                with self.assertRaises(EvidenceAuditError):
                    build_evidence_audit(self.candidates, self.validation, evidence, report_file="report.md", generated_at="now")

    def test_secrets_and_url_credentials_are_redacted_or_removed(self):
        secret = "gsk-secret-value-123456"
        url = "https://user:password@example.com/story?api_key=gsk-secret-value-123456&view=full"
        source = {
            "title": f"Nimbus {secret}", "url": url,
            "snippet": f"Nimbus announced Atlas. {secret} Authorization: Bearer private-header; Cookie: session-id=private-cookie.",
            "publication_date": "",
        }
        claim = ValidatedClaim(f"Nimbus announced Atlas. {secret}", url, "Nimbus announced Atlas.")
        story = ValidatedStory("Nimbus announces Atlas", "Nimbus announced Atlas.", "Not verified", [url], claims=[claim])
        payload = build_evidence_audit(
            [{"headline": story.headline}], StoryValidationResult(accepted=[story]), [source],
            report_file="report.md", generated_at="now", secrets=[secret],
        )
        serialized = json.dumps(payload)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("user:password", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("private-header", serialized)
        self.assertNotIn("private-cookie", serialized)
        self.assertIn("view=full", serialized)

    def test_source_fields_remain_bounded(self):
        source = dict(self.source, title="T" * 500, snippet="Nimbus announced Atlas. " + ("S" * 500))
        story = ValidatedStory(
            self.story.headline, self.story.summary, self.story.publication_date,
            self.story.source_urls, claims=[ValidatedClaim(
                self.claim.claim_text, self.claim.source_url, "Nimbus announced Atlas."
            )],
        )
        payload = build_evidence_audit(
            self.candidates, StoryValidationResult(accepted=[story]), [source],
            report_file="report.md", generated_at="now",
        )
        claim_record = payload["accepted_stories"][0]["claims"][0]
        self.assertLessEqual(len(claim_record["source_title"]), 300)
        self.assertLessEqual(len(claim_record["source_snippet"]), 300)

    def test_rejected_claims_keep_only_sanitized_diagnostics(self):
        validation = StoryValidationResult(claim_rejections=[{
            "candidate_index": 1, "claim_index": 2, "reason_code": "claim_excerpt_not_found",
        }])
        candidate = {"headline": "Nimbus report", "claims": [{"claim_text": "private text", "source_url": "https://example.com/?token=secret"}]}
        payload = build_evidence_audit([candidate], validation, [], report_file="report.md", generated_at="now")
        rejection = payload["rejected_claims"][0]
        self.assertEqual(rejection["reason_code"], "claim_excerpt_not_found")
        self.assertNotIn("claim_text", rejection)
        self.assertNotIn("source_url", rejection)


if __name__ == "__main__":
    unittest.main()
