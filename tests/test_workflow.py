import unittest
from pathlib import Path


class BotWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(".github/workflows/bot.yml").read_text(encoding="utf-8")

    def test_long_polling_window_has_room_for_clean_handoff(self):
        self.assertIn("timeout-minutes: 58", self.workflow)
        self.assertIn("RUN_SECONDS: '3000'", self.workflow)

    def test_successor_is_dispatched_even_after_transient_failure(self):
        self.assertIn("actions: write", self.workflow)
        self.assertIn("name: Queue next polling window", self.workflow)
        self.assertIn("if: ${{ always()", self.workflow)
        self.assertIn('gh workflow run bot.yml --ref "$GITHUB_REF_NAME"', self.workflow)

    def test_cron_is_recovery_not_the_primary_transport(self):
        self.assertIn("cron: '*/30 * * * *'", self.workflow)
        self.assertIn("concurrency:", self.workflow)
        self.assertIn("group: court-telegram-poller", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)


if __name__ == "__main__":
    unittest.main()
