import hashlib
import re
from datetime import timedelta

BASE_ROLES = [
    ("judge", "Судья", None),
    ("prosecutor", "Прокурор", "prosecution"),
    ("defense", "Адвокат", "defense"),
    ("accused", "Подсудимый", "defense"),
    ("witness_1", "Свидетель 1", None),
    ("witness_2", "Свидетель 2", None),
    ("juror_1", "Присяжный 1", None),
    ("juror_2", "Присяжный 2", None),
    ("juror_3", "Присяжный 3", None),
]
PHASES = ("crime", "accusation", "defense", "debate", "final", "verdict")
DURATIONS = {
    "crime": 60,
    "accusation": 150,
    "defense": 150,
    "debate": 150,
    "final": 60,
    "verdict": 150,
}
LABELS = {
    "crime": "Преступление",
    "accusation": "Обвинение",
    "defense": "Защита",
    "debate": "Прения",
    "final": "Финальное слово",
    "verdict": "Вердикт",
}
SPEAKERS = {
    "crime": "judge",
    "accusation": "prosecutor",
    "defense": "defense",
    "debate": "prosecutor",
    "final": "accused",
    "verdict": "judge",
}


def role_specs(count):
    return BASE_ROLES + [
        (f"witness_{i + 3}", f"Свидетель {i + 3}", None)
        for i in range(max(0, count - len(BASE_ROLES)))
    ]


def extend_deadline(created, deadline, seconds):
    if seconds <= 0:
        raise ValueError("Укажите положительное число секунд")
    return min(created + timedelta(minutes=5), deadline + timedelta(seconds=seconds))


def fingerprint(description):
    return hashlib.sha256(
        re.sub(r"\W+", "", description.casefold()).encode()
    ).hexdigest()


def validate_case(data, codes):
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("crime"), str)
        or not data["crime"].strip()
    ):
        raise ValueError("Нет описания дела")
    if data.get("truth") not in ("guilty", "innocent"):
        raise ValueError("Нет скрытого решения")
    clues = data.get("evidence")
    if not isinstance(clues, list) or not 5 <= len(clues) <= 7:
        raise ValueError("Нужно 5–7 улик")
    for clue in clues:
        if not isinstance(clue, dict) or not all(
            isinstance(clue.get(k), str) and clue[k].strip()
            for k in ("title", "content", "interpretation")
        ):
            raise ValueError("Неверная улика")
        visible = clue.get("visible_to_roles")
        if (
            not isinstance(visible, list)
            or not visible
            or not all(v in codes or v == "public" for v in visible)
        ):
            raise ValueError("Неверная видимость")
    secrets = data.get("secrets")
    if not isinstance(secrets, dict) or any(
        not isinstance(secrets.get(c), str) for c in codes
    ):
        raise ValueError("Нужна информация для каждой роли")
    return data
