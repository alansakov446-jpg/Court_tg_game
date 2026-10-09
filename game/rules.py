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


# Fallback case: deterministic template so a room always starts, even when the
# AI cascade is down. The hidden solution is "innocent": the diamond left by
# mistake, not stolen. Only `truth` holds the verdict.
FALLBACK_CRIME = (
    "Дело №{game_id}: со склада аукционного дома «Восток» исчез коллекционный "
    "алмаз «Полярная звезда» во время вечерней приёмки груза. Подозревается "
    "кладовщик — последний, кто работал с коробкой."
)
FALLBACK_EVIDENCE = [
    {
        "title": "Журнал приёмки",
        "content": "В 19:40 кладовщик расписался в приёме партии №44.",
        "interpretation": "Запись сделана до исчезновения; коробка с алмазом была отложена отдельно.",
        "visible_to_roles": ["public"],
    },
    {
        "title": "Камера у ворот",
        "content": "В 20:05 со склада уехал грузовик с партией №51, направлявшейся в порт.",
        "interpretation": "Именно в партии №51 случайно уехал алмаз.",
        "visible_to_roles": ["public"],
    },
    {
        "title": "Осмотр сейфа",
        "content": "Замок сейфа не вскрыт, следов взлома нет.",
        "interpretation": "Исчезновение не было кражей со взломом.",
        "visible_to_roles": ["public"],
    },
    {
        "title": "Показания грузчика",
        "content": "Перед погрузкой у дальнего стеллажа стояла отложенная коробка.",
        "interpretation": "Отложенная коробка — и есть пропавший алмаз.",
        "visible_to_roles": ["witness_1"],
    },
    {
        "title": "Звонок логиста",
        "content": "В 19:55 логист требовал срочно отправить партию №51 без пересчёта мест.",
        "interpretation": "Срочность привела к ошибке комплектации.",
        "visible_to_roles": ["witness_2"],
    },
    {
        "title": "Акт инвентаризации",
        "content": "Партия №51 опечатана без повторной проверки состава.",
        "interpretation": "Алмаз уехал в порт по ошибке, а не был украден.",
        "visible_to_roles": ["judge"],
    },
]
FALLBACK_SECRETS = {
    "judge": "Ты знаешь, что инвентаризация партии №51 проводилась с нарушением процедуры. Сам ты не видел алмаз после 19:40.",
    "prosecutor": "Ты уверен, что подозрение падает на кладовщика: он последний работал с коробкой. Прямых доказательств кражи у тебя нет.",
    "defense": "По журналу охраны подзащитный не покидал пост после 20:00 — это твоё главное алиби.",
    "accused": "Ты отложил коробку с алмазом у стеллажа, чтобы перепаковать её утром; кто убрал её в партию №51 — ты не видел.",
    "witness_1": "Ты видел, как кладовщик откладывал небольшую коробку у дальнего стеллажа перед погрузкой.",
    "witness_2": "Ты слышал звонок логиста: просили срочно отправить партию №51 без пересчёта.",
    "juror_1": "Личных фактов у тебя нет — суди по уликам и показаниям.",
    "juror_2": "Личных фактов у тебя нет — суди по уликам и показаниям.",
    "juror_3": "Личных фактов у тебя нет — суди по уликам и показаниям.",
}


def fallback_case(codes, game_id):
    """Deterministic case that always passes validate_case; unique per game.

    The description embeds the game id so the SHA-256 uniqueness check and the
    unique `case_hash` column never collide between rooms.
    """
    code_set = set(codes)
    evidence = []
    for clue in FALLBACK_EVIDENCE:
        visible = [v for v in clue["visible_to_roles"] if v == "public" or v in code_set]
        evidence.append({**clue, "visible_to_roles": visible or ["public"]})
    secrets = {
        code: FALLBACK_SECRETS.get(
            code, "Ты видел(а) часть вечерней погрузки и суету вокруг партии №51."
        )
        for code in codes
    }
    return validate_case(
        {
            "crime": FALLBACK_CRIME.format(game_id=game_id),
            "truth": "innocent",
            "evidence": evidence,
            "secrets": secrets,
        },
        list(codes),
    )


def explain_failure(exc):
    """Specific but player-friendly reason; never raw provider text or keys."""
    summary = getattr(exc, "summary", "") or ""
    text = summary or type(exc).__name__
    known = [
        ("no_keys_configured", "ключ ИИ не настроен"),
        ("API key not valid", "ключ ИИ недействителен"),
        ("http=404", "модель ИИ больше недоступна (HTTP 404)"),
        ("http=403", "доступ к ИИ запрещён (HTTP 403)"),
        ("http=429", "квоты ИИ исчерпаны (HTTP 429)"),
        ("all_keys_in_cooldown", "квоты ИИ исчерпаны"),
        ("blocked=", "запрос к ИИ заблокирован фильтром безопасности"),
        ("invalid_case_drafts", "ИИ прислал дело с ошибками структуры"),
        ("duplicate_case", "ИИ повторил уже известное дело"),
        ("timeout=", "ИИ не ответил вовремя"),
        ("network=", "нет связи с ИИ"),
        ("TimeoutError", "ИИ не ответил вовремя"),
        ("http=400", "ИИ отклонил запрос (HTTP 400)"),
        ("http=", "ошибка сервиса ИИ"),
    ]
    for marker, phrase in known:
        if marker in text:
            return phrase
    return "ИИ не смог подготовить дело"
