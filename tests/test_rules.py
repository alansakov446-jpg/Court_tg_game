import unittest
from datetime import datetime, timedelta, timezone

from game.rules import (
    explain_failure,
    extend_deadline,
    fallback_case,
    fingerprint,
    role_specs,
    validate_case,
)


class RulesTests(unittest.TestCase):
    def test_lobby_extension_is_capped(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(
            extend_deadline(start, start + timedelta(seconds=150), 999),
            start + timedelta(minutes=5),
        )
        with self.assertRaises(ValueError):
            extend_deadline(start, start, -1)

    def test_roles_never_limit_humans(self):
        self.assertEqual(len(role_specs(9)), 9)
        roles = role_specs(50)
        self.assertEqual(len(roles), 50)
        self.assertEqual(len({r[0] for r in roles}), 50)
        self.assertEqual(sum(r[0].startswith("juror_") for r in roles), 3)
        self.assertTrue(all(r[0].startswith("witness_") for r in roles[9:]))

    def test_fingerprint_ignores_punctuation_and_case(self):
        self.assertEqual(fingerprint("Кража! Ножа."), fingerprint("кража ножа"))

    def test_case_schema_rejects_unknown_visibility(self):
        case = {
            "crime": "Кража",
            "truth": "innocent",
            "evidence": [
                {
                    "title": "След",
                    "content": "Факт",
                    "interpretation": "Версия",
                    "visible_to_roles": ["public"],
                }
                for _ in range(5)
            ],
            "secrets": {"judge": "Личный факт"},
        }
        self.assertIs(validate_case(case, ["judge"]), case)
        case["evidence"][0]["visible_to_roles"] = ["admin"]
        with self.assertRaises(ValueError):
            validate_case(case, ["judge"])

    def test_missing_secrets_rejected(self):
        with self.assertRaises(ValueError):
            validate_case(
                {"crime": "Дело", "truth": "innocent", "evidence": []}, ["judge"]
            )

    def test_fallback_case_passes_validation_for_any_table(self):
        for count in (9, 11, 13):
            codes = [spec[0] for spec in role_specs(count)]
            case = fallback_case(codes, 7)
            self.assertIs(validate_case(case, codes), case)
            self.assertEqual(set(case["secrets"]), set(codes))
            self.assertEqual(case["truth"], "innocent")
            public = sum(
                "public" in clue["visible_to_roles"] for clue in case["evidence"]
            )
            self.assertGreaterEqual(public, 2)

    def test_fallback_case_is_unique_per_game(self):
        codes = [spec[0] for spec in role_specs(9)]
        self.assertNotEqual(
            fingerprint(fallback_case(codes, 1)["crime"]),
            fingerprint(fallback_case(codes, 2)["crime"]),
        )

    def test_explain_failure_names_the_specific_reason(self):
        class Fake(Exception):
            pass

        for summary, expected in [
            ("http=404 NOT_FOUND x5", "модель ИИ больше недоступна (HTTP 404)"),
            ("http=429 RESOURCE_EXHAUSTED x5", "квоты ИИ исчерпаны (HTTP 429)"),
            ("invalid_case_drafts=Нужно 5–7 улик x3", "ИИ прислал дело с ошибками структуры"),
            ("no_keys_configured", "ключ ИИ не настроен"),
        ]:
            exc = Fake("boom")
            exc.summary = summary
            self.assertEqual(explain_failure(exc), expected)
        timeout = TimeoutError()
        self.assertEqual(explain_failure(timeout), "ИИ не ответил вовремя")
