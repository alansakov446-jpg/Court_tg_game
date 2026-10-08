import unittest

from ai.gemini import AIUnavailable
from ai.host import create_case, plan_action
from game.rules import role_specs

CODES = [spec[0] for spec in role_specs(9)]


def valid_case():
    return {
        "crime": "Пропажа картины во время аукциона",
        "truth": "innocent",
        "evidence": [
            {
                "title": f"Улика {i}",
                "content": "Наблюдаемый факт",
                "interpretation": "Скрытая трактовка",
                "visible_to_roles": ["public"] if i < 2 else [CODES[4]],
            }
            for i in range(6)
        ],
        "secrets": {code: "Личные факты роли" for code in CODES},
    }


class FakeAI:
    """Returns prepared answers; exceptions are raised like the real cascade."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    async def generate(self, prompt, *, json_mode=False):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_case_is_accepted(self):
        ai = FakeAI([valid_case()])
        with self.assertLogs("court.ai.host", level="INFO") as logs:
            self.assertEqual(await create_case(ai, CODES, []), valid_case())
        self.assertEqual(ai.calls, 1)
        self.assertIn("Case draft accepted attempt=1/3", "\n".join(logs.output))

    async def test_invalid_drafts_are_logged_with_local_reasons(self):
        ai = FakeAI([{"crime": "Дело без решения"}])
        with (
            self.assertLogs("court.ai.host", level="WARNING") as logs,
            self.assertRaises(AIUnavailable) as cm,
        ):
            await create_case(ai, CODES, [])
        self.assertEqual(ai.calls, 3)
        output = "\n".join(logs.output)
        self.assertIn("Case draft rejected attempt=1/3 roles=9", output)
        self.assertIn("Case draft rejected attempt=3/3", output)
        self.assertIn("Нет скрытого решения", output)
        self.assertIn("Case generation failed: 3 invalid drafts", output)
        self.assertEqual(
            cm.exception.summary, "invalid_case_drafts=Нет скрытого решения x3"
        )

    async def test_provider_failure_keeps_its_summary(self):
        ai = FakeAI([AIUnavailable("ИИ временно недоступен", summary="http=429 x10")])
        with self.assertRaises(AIUnavailable) as cm:
            await create_case(ai, CODES, [])
        self.assertEqual(ai.calls, 1)  # the cascade is already exhausted
        self.assertEqual(cm.exception.summary, "http=429 x10")

    async def test_non_object_action_plan_is_reported(self):
        ai = FakeAI([["expert"]])
        with self.assertLogs("court.ai.host", level="WARNING") as logs:
            self.assertEqual(
                await plan_action(ai, "Адвокат", {}, [], 50, 25), {"action": "none"}
            )
        self.assertIn("Action plan is not an object (got list)", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
