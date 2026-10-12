import os
import unittest
from unittest.mock import patch

from config import ConfigurationError, load_settings


class ConfigTests(unittest.TestCase):
    def test_missing_keys_are_reported(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "", "TAVILY_API_KEY": ""}, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "GROQ_API_KEY, TAVILY_API_KEY"):
                load_settings()

    def test_valid_configuration_and_bounds(self):
        env = {"GROQ_API_KEY": "groq-test", "TAVILY_API_KEY": "tavily-test", "MAX_SEARCHES": "2"}
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings()
        self.assertEqual(settings.max_searches, 2)
        self.assertEqual(settings.max_results, 3)
        self.assertEqual(settings.timezone_name, "Asia/Karachi")
        self.assertFalse(settings.evidence_audit_enabled)

    def test_evidence_audit_can_be_enabled_or_disabled(self):
        base = {"GROQ_API_KEY": "groq-test", "TAVILY_API_KEY": "tavily-test"}
        with patch.dict(os.environ, {**base, "EVIDENCE_AUDIT_ENABLED": "true"}, clear=True):
            self.assertTrue(load_settings().evidence_audit_enabled)
        with patch.dict(os.environ, {**base, "EVIDENCE_AUDIT_ENABLED": "off"}, clear=True):
            self.assertFalse(load_settings().evidence_audit_enabled)

    def test_invalid_evidence_audit_setting_is_rejected(self):
        env = {"GROQ_API_KEY": "groq-test", "TAVILY_API_KEY": "tavily-test", "EVIDENCE_AUDIT_ENABLED": "sometimes"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "EVIDENCE_AUDIT_ENABLED must be a boolean"):
                load_settings()

    def test_search_limit_cannot_exceed_two(self):
        env = {"GROQ_API_KEY": "x", "TAVILY_API_KEY": "y", "MAX_SEARCHES": "3"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "between 1 and 2"):
                load_settings()

    def test_invalid_timezone(self):
        env = {"GROQ_API_KEY": "x", "TAVILY_API_KEY": "y", "APP_TIMEZONE": "Mars/Olympus"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ConfigurationError, "IANA timezone"):
                load_settings()
