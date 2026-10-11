import unittest
from pathlib import Path


class BotWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = Path(".github/workflows/bot.yml").read_text(encoding="utf-8")

    def test_long_polling_shift_has_room_for_clean_handoff(self):
        self.assertIn("timeout-minutes: 350", self.workflow)
        self.assertIn("RUN_SECONDS: '19800'", self.workflow)

    def test_successor_is_dispatched_without_waiting_for_cron(self):
        self.assertIn("actions: write", self.workflow)
        self.assertIn("name: Start the next shift", self.workflow)
        self.assertIn("/actions/workflows/bot.yml/dispatches", self.workflow)

    def test_cron_and_concurrency_are_recovery_guards(self):
        self.assertIn("cron: '*/5 * * * *'", self.workflow)
        self.assertIn("concurrency:", self.workflow)
        self.assertIn("group: court-telegram-poller", self.workflow)
        self.assertIn("cancel-in-progress:", self.workflow)

    def test_poller_reconnects_inside_a_shift(self):
        self.assertIn("health.last_ok.json", self.workflow)
        self.assertIn("reconnecting in 5s", self.workflow)


if __name__ == "__main__":
    unittest.main()
