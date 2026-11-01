"""Tests for the config check in list_models.py (no SDK call)."""
import sys
import types
import unittest
from types import SimpleNamespace as NS

# list_models imports the Copilot SDK at module load; stub it for the test.
sys.modules.setdefault("copilot", types.SimpleNamespace(CopilotClient=object))

from config import HarnessConfig  # noqa: E402
from list_models import config_rows, print_config_check  # noqa: E402


def _model(mid, vision):
    return NS(id=mid, name=mid,
              capabilities=NS(supports=NS(vision=vision), limits=NS(vision=None)))


MODELS = [_model("gpt-5.3-codex", True), _model("text-only", False)]


class ConfigRowsTest(unittest.TestCase):
    def _cfg(self, **kw):
        cfg = HarnessConfig()
        cfg.model = kw.get("model", "gpt-5.3-codex")
        cfg.phase_models = kw.get("phase_models", {})
        cfg.review_model = kw.get("review_model", "")
        cfg.skip_phases = kw.get("skip_phases", [])
        cfg.attachment_phases = kw.get("attachment_phases", [])
        return cfg

    def _by_phase(self, cfg, phases):
        return {r["phase"]: r for r in config_rows(cfg, MODELS, phases)}

    def test_missing_model(self):
        rows = self._by_phase(self._cfg(phase_models={"context": "claude-opus-4.8"}),
                              ["context", "coding"])
        self.assertEqual(rows["context"]["status"], "missing")
        self.assertEqual(rows["coding"]["status"], "ok")

    def test_review_model_used_for_code_review(self):
        rows = self._by_phase(self._cfg(review_model="nope"), ["code_review"])
        self.assertEqual(rows["code_review"]["model"], "nope")
        self.assertEqual(rows["code_review"]["status"], "missing")

    def test_no_vision_only_when_phase_gets_attachments(self):
        cfg = self._cfg(model="text-only", attachment_phases=["coding"])
        rows = self._by_phase(cfg, ["context", "coding"])
        self.assertEqual(rows["context"]["status"], "ok")
        self.assertEqual(rows["coding"]["status"], "no-vision")

    def test_auto_and_skip(self):
        rows = self._by_phase(self._cfg(model="auto", skip_phases=["unit_testing"]),
                              ["context", "unit_testing"])
        self.assertEqual(rows["context"]["status"], "auto")
        self.assertNotIn("unit_testing", rows)

    def test_problem_count(self):
        cfg = self._cfg(phase_models={"context": "x"}, model="text-only",
                        attachment_phases=["coding"])
        rows = config_rows(cfg, MODELS, ["context", "coding", "validation"])
        self.assertEqual(print_config_check(rows, in_actions=False), 2)


if __name__ == "__main__":
    unittest.main()
