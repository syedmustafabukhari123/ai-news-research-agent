import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from report import (
    ReportValidationError,
    StoryCandidate,
    StructuredResearchOutput,
    render_structured_report,
    save_structured_report,
    validate_story_items,
)


def source(title, url, snippet, publication_date=""):
    return {"title": title, "url": url, "snippet": snippet, "publication_date": publication_date}


def claim(text, url, excerpt):
    return {"claim_text": text, "source_url": url, "evidence_excerpt": excerpt}


def candidate(headline, summary, url, excerpt, **kwargs):
    return StoryCandidate(
        headline=headline, summary=summary, source_urls=[url],
        claims=[claim(summary or "", url, excerpt)], **kwargs,
    )


class StructuredReportTests(unittest.TestCase):
    def test_valid_structured_output_parses(self):
        output = StructuredResearchOutput.model_validate({"stories": [{
            "headline": "Nimbus announces Atlas",
            "summary": "Nimbus announced Atlas for developers.",
            "publication_date": None,
            "source_urls": ["https://example.com/atlas"],
            "interpretation": None,
            "claims": [{"claim_text": "Nimbus announced Atlas for developers.", "source_url": "https://example.com/atlas", "evidence_excerpt": "Nimbus announced Atlas for developers."}],
        }]})
        self.assertEqual(output.stories[0].headline, "Nimbus announces Atlas")

    def test_malformed_json_and_unsupported_structure_fail_closed(self):
        with self.assertRaises(ValidationError):
            StructuredResearchOutput.model_validate_json('{"stories": [')
        with self.assertRaises(ValidationError):
            StructuredResearchOutput.model_validate({"report": "not structured output"})

    def test_missing_headline_summary_and_source_urls_have_reason_codes(self):
        candidates = [
            {"headline": None, "summary": "Nimbus announced Atlas.", "source_urls": ["https://example.com/a"]},
            {"headline": "Nimbus announces Atlas", "summary": None, "source_urls": ["https://example.com/a"]},
            {"headline": "Nimbus announces Atlas", "summary": "Nimbus announced Atlas.", "source_urls": []},
        ]
        evidence = [source("Nimbus announces Atlas", "https://example.com/a", "Nimbus announced Atlas.")]
        result = validate_story_items(candidates, evidence)
        self.assertEqual([item.reason_code for item in result.rejected], [
            "missing_headline", "missing_summary", "missing_source_urls",
        ])
        self.assertEqual(result.accepted, [])

    def test_mixed_valid_and_invalid_candidates_preserve_valid_story(self):
        evidence = [source("Nimbus announces Atlas", "https://example.com/a", "Nimbus announced Atlas.")]
        result = validate_story_items([
            candidate("Nimbus announces Atlas", "Nimbus announced Atlas.", "https://example.com/a", "Nimbus announced Atlas."),
            StoryCandidate(headline="", summary="No headline here.", source_urls=["https://example.com/a"]),
        ], evidence)
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual([item.reason_code for item in result.rejected], ["missing_headline"])

    def test_duplicate_story_merges_supporting_urls_and_is_not_counted_twice(self):
        one = "https://news.example.com/nimbus-atlas"
        two = "https://nimbus.example.com/atlas-announcement"
        evidence = [
            source("Nimbus launches Atlas research model", one, "Nimbus launched Atlas research model for developers."),
            source("Nimbus releases Atlas research model", two, "Nimbus launched Atlas research model for developers."),
        ]
        result = validate_story_items([
            candidate("Nimbus launches Atlas research model", "Nimbus launched Atlas research model for developers.", one, "Nimbus launched Atlas research model for developers."),
            candidate("Nimbus releases Atlas research model", "Nimbus launched Atlas research model for developers.", two, "Nimbus launched Atlas research model for developers."),
        ], evidence)
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(set(result.accepted[0].source_urls), {one, two})
        self.assertEqual(result.rejected[0].reason_code, "duplicate_story_merged")

    def test_distinct_stories_from_same_organization_remain_distinct(self):
        model_url = "https://example.com/nimbus-atlas"
        funding_url = "https://example.com/nimbus-funding"
        evidence = [
            source("Nimbus releases Atlas model", model_url, "Nimbus released Atlas model."),
            source("Nimbus raises funding round", funding_url, "Nimbus raised a funding round."),
        ]
        result = validate_story_items([
            candidate("Nimbus releases Atlas model", "Nimbus released Atlas model.", model_url, "Nimbus released Atlas model."),
            candidate("Nimbus raises funding round", "Nimbus raised a funding round.", funding_url, "Nimbus raised a funding round."),
        ], evidence)
        self.assertEqual(len(result.accepted), 2)
        self.assertEqual(result.rejected, [])

    def test_unsupported_numerical_and_technical_claims_are_rejected(self):
        numeric_url = "https://example.com/numeric"
        technical_url = "https://example.com/bench"
        evidence = [
            source("Nimbus announces Atlas", numeric_url, "Nimbus announced Atlas for developers."),
            source("Android Bench 2.0 evaluates developer tasks", technical_url, "The benchmark evaluates Android development tasks."),
        ]
        result = validate_story_items([
            candidate("Nimbus announces Atlas 500", "Nimbus announced Atlas for developers.", numeric_url, "Nimbus announced Atlas for developers."),
            candidate("Android Bench 2.0", "The benchmark measures on-device AI inference workloads.", technical_url, "The benchmark evaluates Android development tasks."),
        ], evidence)
        self.assertEqual([item.reason_code for item in result.rejected], [
            "unsupported_numerical_claim", "unsupported_technical_claim",
        ])

    def test_publication_date_mismatch_is_rejected(self):
        url = "https://example.com/dated"
        result = validate_story_items([
            StoryCandidate(
                headline="Nimbus announces Atlas", summary="Nimbus announced Atlas model.", claims=[claim("Nimbus announced Atlas model.", url, "Nimbus announced Atlas model.")],
                publication_date="12 Mar 2025", source_urls=[url],
            )
        ], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas model.", "Tue, 11 Mar 2025 17:00:00 GMT")])
        self.assertEqual(result.rejected[0].reason_code, "publication_date_mismatch")

    def test_missing_date_becomes_not_verified_and_unverified_claim_is_warned(self):
        url = "https://example.com/undated"
        result = validate_story_items([
            StoryCandidate(
                headline="Nimbus announces Atlas", summary="Nimbus announced Atlas model.", claims=[claim("Nimbus announced Atlas model.", url, "Nimbus announced Atlas model.")],
                publication_date="12 Mar 2025", source_urls=[url],
            )
        ], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas model.")])
        self.assertEqual(result.accepted[0].publication_date, "Not verified")
        self.assertEqual(result.warnings[0]["reason_code"], "publication_date_unverified")

    def test_source_url_must_be_valid_and_retrieved(self):
        evidence = [source("Nimbus announces Atlas", "https://example.com/a", "Nimbus announced Atlas.")]
        invalid = validate_story_items([
            candidate("Nimbus announces Atlas", "Nimbus announced Atlas.", "http:///bad", "Nimbus announced Atlas." )
        ], evidence)
        missing = validate_story_items([
            candidate("Nimbus announces Atlas", "Nimbus announced Atlas.", "https://other.example/a", "Nimbus announced Atlas." )
        ], evidence)
        self.assertEqual(invalid.rejected[0].reason_code, "invalid_source_url")
        self.assertEqual(missing.rejected[0].reason_code, "source_not_retrieved")

    def test_zero_valid_stories_does_not_render_a_report(self):
        with self.assertRaisesRegex(ReportValidationError, "No stories passed"):
            render_structured_report([], report_date="2026-10-12", research_window="window", generated_at="time")

    def test_markdown_rendering_is_deterministic_and_includes_verified_fields(self):
        story = validate_story_items([
            candidate("Nimbus announces Atlas", "Nimbus announced Atlas model.", "https://example.com/a", "Nimbus announced Atlas model.", interpretation="This may interest developers.")
        ], [source("Nimbus announces Atlas", "https://example.com/a", "Nimbus announced Atlas model.", "Tue, 11 Mar 2025 17:00:00 GMT")]).accepted[0]
        kwargs = dict(report_date="2026-10-12", research_window="start through end (UTC)", generated_at="timestamp")
        markdown = render_structured_report([story], **kwargs)
        self.assertEqual(markdown, render_structured_report([story], **kwargs))
        self.assertIn("Publication date (Tavily metadata; not independently verified): Tue, 11 Mar 2025 17:00:00 GMT", markdown)
        self.assertIn("Why it matters (interpretation): This may interest developers.", markdown)
        self.assertIn("- https://example.com/a", markdown)

    def test_structured_report_saves_markdown_from_validated_stories(self):
        url = "https://example.com/a"
        story = validate_story_items([
            candidate("Nimbus announces Atlas", "Nimbus announced Atlas model.", url, "Nimbus announced Atlas model.")
        ], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas model.")]).accepted
        with tempfile.TemporaryDirectory() as temp:
            path = save_structured_report(
                story, Path(temp), "2026-10-12", research_window="window", generated_at="time",
            )
            self.assertIn("## AI Daily News Report", path.read_text(encoding="utf-8"))

    def test_unsupported_gemini_training_claim_is_rejected(self):
        url = "https://tech.example/google-india-hub"
        evidence = [source(
            "Google Defends $15B India AI Hub Amid Water Fears", url,
            "Google said its planned India AI hub would involve a $15 billion investment amid water concerns.",
        )]
        result = validate_story_items([StoryCandidate(
            headline="Google Defends $15B India AI Hub Amid Water Fears",
            summary="The hub trains and serves Gemini models across Asia.", source_urls=[url],
            claims=[claim("Google's India AI hub trains and serves Gemini models across Asia.", url,
                          "Google said its planned India AI hub would involve a $15 billion investment")],
        )], evidence)
        self.assertEqual(result.accepted, [])
        self.assertEqual(result.claim_rejections[0]["reason_code"], "claim_insufficient_overlap")

    def test_exact_retrieved_excerpt_is_accepted_with_whitespace_normalization(self):
        url = "https://example.com/a"
        result = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Model summary deliberately differs.", source_urls=[url],
            claims=[claim("Nimbus announced Atlas for developers.", url, "Nimbus   announced\nAtlas for developers.")],
        )], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas for developers.")])
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].summary, "Nimbus announced Atlas for developers.")
        self.assertNotIn("deliberately differs", result.accepted[0].summary)

    def test_fabricated_excerpt_is_rejected(self):
        url = "https://example.com/a"
        result = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas.", source_urls=[url],
            claims=[claim("Nimbus announced Atlas.", url, "Nimbus launched Atlas worldwide.")],
        )], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas.")])
        self.assertEqual(result.rejected[0].reason_code, "no_supported_claims")
        self.assertEqual(result.claim_rejections[0]["reason_code"], "claim_excerpt_not_found")

    def test_unretrieved_claim_source_is_rejected(self):
        url = "https://example.com/a"
        result = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas.", source_urls=[url],
            claims=[claim("Nimbus announced Atlas.", "https://other.example/a", "Nimbus announces Atlas")],
        )], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas.")])
        self.assertEqual(result.claim_rejections[0]["reason_code"], "claim_source_not_retrieved")

    def test_excerpt_from_wrong_source_is_rejected(self):
        first, second = "https://example.com/a", "https://example.com/b"
        result = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas.", source_urls=[first, second],
            claims=[claim("Nimbus announced Atlas.", second, "Nimbus announced Atlas for developers.")],
        )], [source("Nimbus announces Atlas", first, "Nimbus announced Atlas for developers."),
             source("Garden report", second, "A gardening report was published.")])
        self.assertEqual(result.claim_rejections[0]["reason_code"], "claim_excerpt_not_found")

    def test_mixed_supported_and_unsupported_claims_preserve_supported_fact(self):
        url = "https://example.com/a"
        result = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Unsupported original prose is ignored.", source_urls=[url],
            claims=[
                claim("Nimbus announced Atlas for developers.", url, "Nimbus announced Atlas for developers."),
                claim("Atlas serves every developer in the world.", url, "Nimbus announced Atlas for developers."),
            ],
        )], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas for developers.")])
        self.assertEqual(len(result.accepted), 1)
        self.assertEqual(result.accepted[0].summary, "Nimbus announced Atlas for developers.")
        self.assertEqual(len(result.claim_rejections), 1)

    def test_story_with_zero_accepted_claims_is_rejected(self):
        url = "https://example.com/a"
        result = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas.", source_urls=[url], claims=[],
        )], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas.")])
        self.assertEqual(result.rejected[0].reason_code, "no_supported_claims")

    def test_tavily_date_is_provider_metadata_and_missing_date_is_not_verified(self):
        url = "https://example.com/a"
        supplied = "Tue, 11 Mar 2025 17:00:00 GMT"
        candidate_with_date = StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas.", publication_date="11 Mar 2025",
            source_urls=[url], claims=[claim("Nimbus announced Atlas.", url, "Nimbus announced Atlas.")],
        )
        result = validate_story_items([candidate_with_date], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas.", supplied)])
        self.assertEqual(result.accepted[0].publication_date, supplied)
        undated = validate_story_items([candidate_with_date], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas.")])
        self.assertEqual(undated.accepted[0].publication_date, "Not verified")

    def test_renderer_uses_only_validated_claim_summary(self):
        url = "https://example.com/a"
        story = validate_story_items([StoryCandidate(
            headline="Nimbus announces Atlas", summary="Unsupported prose must disappear.", source_urls=[url],
            claims=[claim("Nimbus announced Atlas for developers.", url, "Nimbus announced Atlas for developers.")],
        )], [source("Nimbus announces Atlas", url, "Nimbus announced Atlas for developers.")]).accepted[0]
        markdown = render_structured_report([story], report_date="2026-10-12", research_window="window", generated_at="time")
        self.assertIn("Summary: Nimbus announced Atlas for developers.", markdown)
        self.assertNotIn("Unsupported prose", markdown)


if __name__ == "__main__":
    unittest.main()
