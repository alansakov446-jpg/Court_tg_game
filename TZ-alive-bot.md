# ТЗ для следующего агента: «Зелёный Actions ≠ живой бот»

**Симптом:** запуск workflow **Court bot** горит зелёным, но бот в Telegram не отвечает и партии умирают. Галочка сегодня = «шаги дошли до конца», а не «бот работал».

**Точка отсчёта:** в `main` влиты PR #5–#9 (диагностика ИИ, модели `gemini-3.8-flash`/`gemini-3.5-flash-lite`, slim-конфиг, cooldown модели, фолбэк-дело, AI-проба, Case lifecycle). Проблема **чисто транспортная** — не ИИ и не генерация дел. Игнорировать всё, что уже работает (см. п.5).

## 1. Причины «зелёный, но мёртвый»

1. **Мёртвое время между окнами.** Поллер живёт `RUN_SECONDS=260` за запуск; cron `*/5` реально откладывается планировщиком GitHub — замерено 09.10.2026: интервалы **3 ч 24 м / 7 ч 28 м / 6 ч 34 м**. Между окнами `getUpdates` никто не вызывает, сообщения копятся в очереди Telegram. Игра на секундах (лобби 150 с, фазы 60–150 с, тишина 30/60 с) — партия умирает гарантированно.

2. **Тихий выход с кодом 0.** `main.py`: `pg_try_advisory_lock(742190018)` не взят → `Another worker is active; exiting` → `return` → **зелёный run без единого поллинга**. Любой ранний `return` так же.

3. **Хрупкий offset.** `.offset` в `actions/cache` (вытесняется) → offset=0 → `getUpdates` отдаёт старые апдейты за 24 ч → повторная обработка старых команд / рассинхрон.

4. **`Explain normal poller exit`** всегда печатает успех; в сводке нет health-цифр и признака «поллер не работал».

## 2. Цель

Зелёный запуск ⇔ за окно был реальный поллинг; бот доступен игрокам в пределах выбранного SLA (рекомендуется — практически непрерывно).

## 3. Критерии приёмки

- **A. Честный красный:** run падает, если 0 циклов `get_updates` за окно (в т.ч. занятый lock), `get_me()` не прошёл, потеряна lock-сессия БД, `poll.log` пуст.

- **B. Health-блок в сводке:** `@username`, `poll cycles`, `updates processed`, `start/stop (UTC)`, `lock=ok`, `offset=<n>`, `pending_lag=<update_id − offset>`.

- **C. Непрерывность — выбрать и реализовать один вариант:**

  - **A (рекомендуется):** долгоживущий сервис `RUN_SECONDS=0` на VPS/Render/Fly/Railway в Docker (`restart: always`) + healthcheck (`GET /healthz` или статус-сообщение в служебный чат); Actions оставить под тесты/alembic/диагностику. SLA: ответ ≤ 15 с круглосуточно.

  - **B (минимум в Actions):** поллер без юнит-тестов в воркфлоу (они уже в `tests.yml`), `RUN_SECONDS=600`, `timeout-minutes ≥ 11`; честно задокументировать окна в README.

- **D. Ручной сценарий:** `/game` → ответ в SLA; `/startnow` → `Case created game=…`; партия проходит все фазы; `/stop` закрывает зал.

- **E. README:** таблица «что означает каждый статус запуска» + SLA.

## 4. Требования

- `main.py`: счётчики `poll_cycles/updates_seen/updates_handled`, файл `health.json`, строка `Health polls=… updates=…`; **код выхода 3** при занятом lock (трактовать как fail по умолчанию); окно без завершённого цикла `get_updates` — неуспешное.

- `bot.yml`: шаг-валидатор (`jq -e '.poll_cycles >= 1'`, иначе `exit 1`), Health-блок в сводку, `Explain normal poller exit` только при `poll_cycles ≥ 1`.

- Offset перенести в БД (`bot_state`, новая alembic-ревизия) или файл рядом с сервисом; `actions/cache` — лишь fallback.

- (Опц.) три подряд пустых окна при ненулевом lag → ERROR `Bot looks dead`.

- **Гигиена (не нарушать):** в логи/сводки/artifacts/check-run не попадают токены, ключи, промпты, ответы ИИ, тексты чатов, имена, `truth` — стражи-тесты уже есть, новые поля проверять ими же.

## 5. Что НЕ переписывать (работает, подтверждено)

Каскад `gemini-3.8-flash` → `gemini-3.5-flash-lite` + slim-конфиг + cooldown модели (`ai/gemini.py`); `fallback_case`/`explain_failure` (`game/rules.py`, `game/service.py`); `scripts/ai_probe.py` + `diagnostics.yml` + блоки AI diagnostics/Case lifecycle (`bot.yml`); игровое ядро `game/*`, `db/models.py`, `alembic/*`.

## 6. Порядок работ

Ветка от актуального `main` → PR; health-счётчики и код 3 (`main.py`); валидатор + Health-блок (`bot.yml`); offset в БД/файле + тесты; (вариант A) деплой-конфиг + README; приёмка: п.3, юниты, ручной сценарий, 2 часа наблюдения.

## 7. Окружение

`BOT_TOKEN`, `DATABASE_URL` (прямой Neon, не `-pooler`), `GEMINI_API` (до 5 ключей). Один poller на токен: advisory lock `742190018` + `concurrency: court-telegram-poller`. Интеграционные тесты — только на одноразовой `TEST_DATABASE_URL`.

## 8. Измеренные факты (09.10.2026)

Интервалы schedule ~3.4–7.5 ч при cron `*/5`; окно 260 с между `Polling started`/`Polling stopped`, между окнами опроса нет; `Another worker is active` даёт зелёный с кодом 0; генерация дел живая (`Case draft accepted attempt=1/3`), при мёртвом ИИ партия стартует на фолбэке — искать «смерть» в транспорте, не в ИИ.
