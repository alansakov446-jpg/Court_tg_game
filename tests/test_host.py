import unittest

from ai.gemini import AIUnavailable
from ai.host import answer_question, create_case, plan_action, speak
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
        self.prompts = []

    async def generate(self, prompt, *, json_mode=False):
        self.prompts.append(prompt)
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

    async def test_host_answers_only_from_supplied_materials(self):
        ai = FakeAI(["В материалах указано: событие произошло на складе."])
        context = {
            "phase": "Обвинение",
            "evidence": [{"id": 1, "title": "Схема", "fact": "Осмотрен склад"}],
            "memory": [],
        }
        answer = await answer_question(
            ai, "Адвокат", "Где это произошло?", context
        )
        self.assertEqual(answer, "В материалах указано: событие произошло на складе.")
        prompt = ai.prompts[0]
        self.assertIn("Где это произошло?", prompt)
        self.assertIn("Осмотрен склад", prompt)
        self.assertIn("только факты", prompt)
        self.assertIn("Не раскрывай скрытую виновность", prompt)

    async def test_empty_host_answer_is_unavailable(self):
        ai = FakeAI(["   "])
        with self.assertRaises(AIUnavailable) as cm:
            await answer_question(ai, "Судья", "Что произошло?", {})
        self.assertEqual(cm.exception.summary, "empty_host_answer")

    async def test_speech_returns_the_text_and_the_done_signal(self):
        ai = FakeAI([{"speech": "Защита доказала алиби.", "done": True}])
        text, done = await speak(ai, "Адвокат", {"phase": "Защита"})
        self.assertEqual(text, "Защита доказала алиби.")
        self.assertTrue(done)
        prompt = ai.prompts[0]
        self.assertIn('"speech"', prompt)
        self.assertIn('"done"', prompt)
        self.assertIn("передать слово", prompt)

    async def test_speech_without_an_explicit_signal_keeps_the_turn(self):
        for answer in ({"speech": "Речь"}, {"speech": "Речь", "done": "yes"}):
            text, done = await speak(FakeAI([answer]), "Прокурор", {})
            self.assertEqual(text, "Речь")
            self.assertFalse(done, answer)
        # A shape the contract does not describe is a failure, not a handover.
        with self.assertRaises(AIUnavailable):
            await speak(FakeAI([["Речь"]]), "Прокурор", {})

    async def test_speech_is_bounded_and_an_empty_answer_is_a_failure(self):
        ai = FakeAI([{"speech": "я" * 900, "done": False}])
        text, _ = await speak(ai, "Прокурор", {})
        self.assertEqual(len(text), 600)
        with self.assertLogs("court.ai.host", level="WARNING") as logs:
            with self.assertRaises(AIUnavailable) as cm:
                await speak(FakeAI([{"speech": "   "}]), "Судья", {})
        self.assertEqual(cm.exception.summary, "empty_speech")
        self.assertIn("Speech answer is unusable", "\n".join(logs.output))

    async def test_non_object_action_plan_is_reported(self):
        ai = FakeAI([["expert"]])
        with self.assertLogs("court.ai.host", level="WARNING") as logs:
            self.assertEqual(
                await plan_action(ai, "Адвокат", {}, [], 50, 25), {"action": "none"}
            )
        self.assertIn("Action plan is not an object (got list)", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
