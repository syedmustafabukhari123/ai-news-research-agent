import json
import unittest

from tools import SearchSession, normalize_search_results


class FakeSearch:
    def __init__(self):
        self.queries = []

    def invoke(self, payload):
        self.queries.append(payload["query"])
        return {"results": [{"title": "Example", "url": "https://example.com", "content": "snippet"}]}


class FailingSearch:
    def invoke(self, payload):
        return {"error": "rate limited"}


class ErrorDictionarySearch:
    def __init__(self, error):
        self.error = error

    def invoke(self, payload):
        return {"error": self.error}


class RaisedSearch:
    def __init__(self, error):
        self.error = error

    def invoke(self, payload):
        raise self.error


class ToolTests(unittest.TestCase):
    def test_successful_response_normalizes_and_counts_without_failure(self):
        session = SearchSession("unused", max_searches=1)
        response = session.build_tool(FakeSearch()).invoke({"query": "AI news"})
        payload = json.loads(response)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(session.calls, 1)
        self.assertEqual(session.failures, 0)

    def test_exact_source_url_duplicates_across_searches_are_suppressed(self):
        class RepeatedSource:
            def invoke(self, payload):
                return {"results": [{"title": "Nimbus Atlas", "url": "https://www.example.com/story/?utm_source=search", "content": "Nimbus released Atlas."}]}

        session = SearchSession("unused", max_searches=2)
        search = session.build_tool(RepeatedSource())
        first = json.loads(search.invoke({"query": "Nimbus Atlas launch"}))
        second = json.loads(search.invoke({"query": "Nimbus Atlas announcement"}))
        self.assertEqual(len(first["results"]), 1)
        self.assertEqual(second["results"], [])
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.failures, 0)
        self.assertEqual(len(session.evidence), 1)
        self.assertEqual(session.evidence[0]["title"], "Nimbus Atlas")
        self.assertEqual(session.evidence[0]["snippet"], "Nimbus released Atlas.")
        self.assertEqual(session.evidence[0]["url"], "https://www.example.com/story/?utm_source=search")

    def test_hard_search_limit_and_duplicate_suppression(self):
        fake = FakeSearch()
        session = SearchSession("unused", max_searches=2, max_results=5)
        search = session.build_tool(fake)
        a = search.invoke({"query": "AI news today"})
        duplicate = search.invoke({"query": "  AI   news TODAY "})
        b = search.invoke({"query": "AI research announcement"})
        limited = search.invoke({"query": "AI agents launch"})
        self.assertEqual(session.calls, 2)
        self.assertEqual(len(fake.queries), 2)
        self.assertIn("already searched", duplicate)
        self.assertIn("Search limit reached", limited)
        self.assertEqual(json.loads(a)["results"][0]["title"], "Example")
        self.assertTrue(b)

    def test_provider_error_is_counted_without_exposing_or_crashing(self):
        session = SearchSession("unused", max_searches=1)
        search = session.build_tool(FailingSearch())
        with self.assertLogs("tools", level="WARNING") as captured:
            response = search.invoke({"query": "AI news"})
        self.assertEqual(session.failures, 1)
        self.assertEqual(session.calls, 1)
        self.assertIn("Tavily search failed", response)
        self.assertIn('"exception_type":null', captured.output[0])

    def test_error_dictionary_extracts_exception_http_status_and_provider_code(self):
        class ProviderFailure(Exception):
            status_code = 401
            code = "invalid_api_key"

        configured_key = "tvly-test-secret-987654"
        error = ProviderFailure(f"Unauthorized; query=latest AI news; Bearer tvly-supersecret; key={configured_key}")
        session = SearchSession(configured_key, max_searches=1)
        with self.assertLogs("tools", level="WARNING") as captured:
            response = session.build_tool(ErrorDictionarySearch(error)).invoke({"query": "latest AI news"})
        line = captured.output[0]
        self.assertIn('"exception_type":"ProviderFailure"', line)
        self.assertIn('"http_status":401', line)
        self.assertIn('"provider_code":"invalid_api_key"', line)
        self.assertIn("[query redacted]", line)
        self.assertNotIn("tvly-supersecret", line)
        self.assertNotIn(configured_key, line)
        self.assertEqual(json.loads(response), {"error": "Tavily search failed"})
        self.assertEqual((session.calls, session.failures), (1, 1))

    def test_raised_http_exception_logs_status_and_returns_concise_error(self):
        class HttpFailure(Exception):
            def __init__(self):
                super().__init__("HTTP 429: too many requests")
                self.response = type("Response", (), {"status_code": 429})()

        session = SearchSession("unused", max_searches=1)
        with self.assertLogs("tools", level="WARNING") as captured:
            response = session.build_tool(RaisedSearch(HttpFailure())).invoke({"query": "AI news"})
        self.assertIn('"exception_type":"HttpFailure"', captured.output[0])
        self.assertIn('"http_status":429', captured.output[0])
        self.assertEqual(json.loads(response), {"error": "Tavily search failed"})
        self.assertEqual((session.calls, session.failures), (1, 1))

    def test_tavily_value_error_parses_http_status_from_message(self):
        session = SearchSession("unused", max_searches=1)
        with self.assertLogs("tools", level="WARNING") as captured:
            session.build_tool(RaisedSearch(ValueError("Error 432: usage limit reached"))).invoke({"query": "AI news"})
        self.assertIn('"http_status":432', captured.output[0])

    def test_network_error_is_classified_without_request_details(self):
        session = SearchSession("unused", max_searches=1)
        with self.assertLogs("tools", level="WARNING") as captured:
            response = session.build_tool(RaisedSearch(ConnectionError("connection refused"))).invoke({"query": "private search phrase"})
        self.assertIn('"exception_type":"ConnectionError"', captured.output[0])
        self.assertIn("connection refused", captured.output[0])
        self.assertNotIn("private search phrase", captured.output[0])
        self.assertEqual(json.loads(response), {"error": "Tavily search failed"})
        self.assertEqual((session.calls, session.failures), (1, 1))

    def test_normalization_keeps_only_sources_and_caps_results(self):
        payload = {"results": [
            {"title": f"Story {i}", "url": f"https://example.com/{i}", "published_date": "2026-10-12", "content": "A" * 500, "score": 0.99, "raw_content": "REMOVE"}
            for i in range(5)
        ], "answer": "REMOVE", "images": ["REMOVE"]}
        normalized = json.loads(normalize_search_results(payload))
        self.assertEqual(len(normalized["results"]), 3)
        self.assertTrue(normalized["truncated"])
        first = normalized["results"][0]
        self.assertEqual(set(first), {"title", "url", "publication_date", "snippet"})
        self.assertEqual(first["url"], "https://example.com/0")
        self.assertEqual(len(first["snippet"]), 300)
        self.assertNotIn("raw_content", first)

    def test_response_size_is_bounded_and_preserves_url(self):
        payload = {"results": [
            {"title": "Story", "url": f"https://example.com/{i}", "content": "context " * 100}
            for i in range(3)
        ]}
        serialized = normalize_search_results(payload, max_response_bytes=700)
        result = json.loads(serialized)
        self.assertLessEqual(len(serialized.encode("utf-8")), 700)
        self.assertTrue(result["results"])
        self.assertTrue(result["results"][0]["url"].startswith("https://"))
        self.assertTrue(result["truncated"])

    def test_missing_publication_date_is_not_fabricated(self):
        normalized = json.loads(normalize_search_results({"results": [{"title": "Undated", "url": "https://example.com", "content": "Snippet"}]}))
        self.assertNotIn("publication_date", normalized["results"][0])

    def test_malformed_fields_are_ignored(self):
        normalized = json.loads(normalize_search_results({"results": [None, {"url": 5}, {"title": "bad", "url": "javascript:alert(1)"}]}))
        self.assertEqual(normalized["results"], [])


if __name__ == "__main__":
    unittest.main()
