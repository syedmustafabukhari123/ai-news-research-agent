import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent import ResearchError, RunUsageTracker, extract_usage_metadata, provider_error_diagnostics, run_research
from config import Settings
from report import StoryCandidate, StructuredResearchOutput


class DiagnosticTests(unittest.TestCase):
    def test_extracts_standard_usage_metadata(self):
        response = SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
            usage_metadata={"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
            response_metadata={},
        ))]])
        self.assertEqual(extract_usage_metadata(response), {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168})

    def test_extracts_groq_usage_metadata_fallback(self):
        response = SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
            usage_metadata=None,
            response_metadata={"token_usage": {"prompt_tokens": 31, "completion_tokens": 9, "total_tokens": 40}},
        ))]])
        self.assertEqual(extract_usage_metadata(response), {"input_tokens": 31, "output_tokens": 9, "total_tokens": 40})

    def test_callback_marks_partial_usage(self):
        tracker = RunUsageTracker("openai/gpt-oss-120b")
        tracker.on_chat_model_start({}, [[]])
        tracker.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
            usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}, response_metadata={}
        ))]]))
        tracker.on_chat_model_start({}, [[]])
        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["model_calls"], 2)
        self.assertEqual(snapshot["usage_calls"], 1)
        self.assertEqual(snapshot["total_tokens"], 5)
        self.assertFalse(snapshot["token_usage_complete"])

    def test_429_diagnostics_capture_safe_headers_and_category(self):
        class FakeRateLimit(Exception):
            status_code = 429
            body = {"error": {"code": "rate_limit_exceeded", "message": "Rate limit on tokens per minute"}}
            response = SimpleNamespace(status_code=429, headers={
                "Retry-After": "42", "x-ratelimit-remaining-tokens": "0",
                "x-ratelimit-reset-tokens": "42s", "Authorization": "secret",
            })

        details = provider_error_diagnostics(FakeRateLimit("429"))
        self.assertEqual(details["provider_status"], 429)
        self.assertEqual(details["rate_limit_category"], "TPM")
        self.assertEqual(details["rate_limit_headers"]["retry-after"], "42")
        self.assertNotIn("authorization", details["rate_limit_headers"])

    def test_429_without_dimension_is_unknown(self):
        class Generic429(Exception):
            status_code = 429
            body = {"error": {"message": "Too many requests"}}

        self.assertEqual(provider_error_diagnostics(Generic429())["rate_limit_category"], "unknown throttling")

    def test_http_hook_records_safe_429_response_for_later_reset(self):
        tracker = RunUsageTracker("openai/gpt-oss-120b")
        tracker.on_http_response(SimpleNamespace(status_code=429, headers={
            "Retry-After": "30", "x-ratelimit-remaining-requests": "0", "Set-Cookie": "secret",
        }))
        reset = ConnectionError("connection reset")
        details = provider_error_diagnostics(reset, prior_http_error=tracker.last_http_error)
        self.assertEqual(details["connection_category"], "connection_error")
        self.assertEqual(details["prior_http_error"]["provider_status"], 429)
        self.assertEqual(details["prior_http_error"]["rate_limit_category"], "RPD")
        self.assertNotIn("set-cookie", details["prior_http_error"]["rate_limit_headers"])

    def test_connection_reset_is_not_a_rate_limit(self):
        reset = ConnectionError("connection reset")
        reset.winerror = 10054
        wrapper = RuntimeError("provider call failed")
        wrapper.__cause__ = reset
        details = provider_error_diagnostics(wrapper)
        self.assertEqual(details["connection_category"], "connection_reset")
        self.assertIsNone(details["provider_status"])
        self.assertIsNone(details["rate_limit_category"])

    def test_run_research_wires_callback_and_returns_usage_counts(self):
        structured = StructuredResearchOutput(stories=[StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas for developers.",
            publication_date=None, source_urls=["https://example.com/atlas"], interpretation=None,
            claims=[{"claim_text": "Nimbus announced Atlas for developers.", "source_url": "https://example.com/atlas", "evidence_excerpt": "Nimbus announced Atlas for developers."}],
        )])

        class FakeAgent:
            invocations = 0
            last_config = None

            def invoke(self, inputs, config):
                self.invocations += 1
                self.last_config = config
                callback = config["callbacks"][0]
                callback.on_chat_model_start({}, [[]])
                callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
                    usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}, response_metadata={}
                ))]]))
                return {"messages": [], "structured_response": structured}

        settings = Settings("test", "test", "openai/gpt-oss-120b", "UTC", Path("reports"), Path("logs"), 4, 3, 30, 24, "INFO")
        with patch("agent.ChatGroq") as chat_groq, patch("agent.create_agent", return_value=FakeAgent()) as create:
            fake_agent = create.return_value
            result = run_research(settings, datetime.now(timezone.utc), datetime.now(timezone.utc))
        self.assertEqual(create.call_args.kwargs["response_format"].schema, StructuredResearchOutput)
        self.assertFalse(create.call_args.kwargs["response_format"].handle_errors)
        self.assertEqual(fake_agent.last_config["recursion_limit"], 7)
        self.assertEqual(chat_groq.call_args.kwargs["max_retries"], 1)
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(fake_agent.invocations, 1)
        self.assertEqual(result.input_tokens, 10)
        self.assertEqual(result.total_tokens, 14)
        self.assertTrue(result.token_usage_complete)
        self.assertEqual(result.parse_status, "parsed")
        self.assertEqual(result.stories[0].headline, "Nimbus announces Atlas")

    def test_run_research_caps_structured_stories_at_three(self):
        stories = [StoryCandidate(
            headline=f"Story {index}", summary=f"Summary {index}", publication_date=None,
            source_urls=[f"https://example.com/{index}"], interpretation=None,
            claims=[{"claim_text": f"Summary {index}", "source_url": f"https://example.com/{index}", "evidence_excerpt": f"Summary {index}"}],
        ) for index in range(4)]
        structured = StructuredResearchOutput(stories=stories)

        class FakeAgent:
            def invoke(self, inputs, config):
                return {"structured_response": structured}

        settings = Settings("test", "test", "openai/gpt-oss-120b", "UTC", Path("reports"), Path("logs"), 2, 3, 30, 24, "INFO")
        with patch("agent.ChatGroq"), patch("agent.create_agent", return_value=FakeAgent()):
            result = run_research(settings, datetime.now(timezone.utc), datetime.now(timezone.utc))
        self.assertEqual([story.headline for story in result.stories], ["Story 0", "Story 1", "Story 2"])

    def test_two_searches_can_be_followed_by_final_structured_response(self):
        structured = StructuredResearchOutput(stories=[StoryCandidate(
            headline="Nimbus announces Atlas", summary="Nimbus announced Atlas for developers.",
            publication_date=None, source_urls=["https://example.com/atlas"], interpretation=None,
            claims=[{"claim_text": "Nimbus announced Atlas for developers.", "source_url": "https://example.com/atlas", "evidence_excerpt": "Nimbus announced Atlas for developers."}],
        )])

        class FakeSearch:
            def __init__(self):
                self.calls = []

            def invoke(self, payload):
                self.calls.append(payload["query"])
                return {"results": [{
                    "title": f"Nimbus Atlas update {len(self.calls)}",
                    "url": f"https://example.com/atlas-{len(self.calls)}",
                    "content": "Nimbus announced Atlas for developers.",
                }]}

        fake_search = FakeSearch()
        search_turns = []

        class FakeAgent:
            def invoke(self, inputs, config):
                self.last_config = config
                search_tool = create.call_args.kwargs["tools"][0]
                search_turns.append(json.loads(search_tool.invoke({"query": "Nimbus Atlas launch"})))
                search_turns.append(json.loads(search_tool.invoke({"query": "Nimbus Atlas developer update"})))
                callback = config["callbacks"][0]
                for _ in range(3):
                    callback.on_chat_model_start({}, [[]])
                    callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(
                        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}, response_metadata={}
                    ))]]))
                return {"messages": [], "structured_response": structured}

        settings = Settings("test", "test", "openai/gpt-oss-120b", "UTC", Path("reports"), Path("logs"), 2, 3, 30, 24, "INFO")
        with patch("agent.ChatGroq"), patch("tools.TavilySearch", return_value=fake_search), patch("agent.create_agent", return_value=FakeAgent()) as create:
            result = run_research(settings, datetime.now(timezone.utc), datetime.now(timezone.utc))

        self.assertEqual(len(fake_search.calls), 2)
        self.assertEqual(len(search_turns), 2)
        self.assertEqual(result.search_calls, 2)
        self.assertEqual(result.model_calls, 3)
        self.assertEqual(result.stories[0].headline, "Nimbus announces Atlas")
        self.assertEqual(create.call_args.kwargs["response_format"].schema, StructuredResearchOutput)
        self.assertEqual(create.return_value.last_config["recursion_limit"], 7)

    def test_unstructured_agent_response_fails_closed_with_safe_status(self):
        class FakeAgent:
            def invoke(self, inputs, config):
                return {"messages": [SimpleNamespace(content="unstructured output")], "structured_response": None}

        settings = Settings("test", "test", "openai/gpt-oss-120b", "UTC", Path("reports"), Path("logs"), 4, 3, 30, 24, "INFO")
        with patch("agent.ChatGroq"), patch("agent.create_agent", return_value=FakeAgent()):
            with self.assertRaises(ResearchError) as caught:
                run_research(settings, datetime.now(timezone.utc), datetime.now(timezone.utc))
        self.assertEqual(caught.exception.output_diagnostics["parse_status"], "parse_failed")
        self.assertIsNone(caught.exception.output_diagnostics["candidate_items"])


if __name__ == "__main__":
    unittest.main()
