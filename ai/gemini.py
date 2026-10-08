"""Gemini cascade with safe diagnostics.

Every failed attempt is logged with its model, HTTP status and provider error code, so
an exhausted cascade is visible in CI logs instead of hiding behind one generic message.
API keys, request bodies, prompts and generated answers are never logged; a provider
error message is redacted of keys and capped.
"""

import json
import logging
import time

import httpx

log = logging.getLogger("court.ai")
MESSAGE_LIMIT = 200  # cap for one provider error message
SUMMARY_LIMIT = 600  # cap for the aggregated cause attached to AIUnavailable
REDACTED = "[redacted]"


class AIUnavailable(RuntimeError):
    """No model/key combination produced a usable answer."""

    def __init__(self, message, *, summary=""):
        super().__init__(message)
        # Technical cause for logs only: models, HTTP statuses, provider codes.
        self.summary = summary


def cause(exc):
    """Compact loggable cause of a failure; never a key, prompt or game text."""
    summary = getattr(exc, "summary", "")
    return f"{type(exc).__name__}: {summary}" if summary else type(exc).__name__


def summarize(attempts):
    """Collapse repeated per-attempt labels into one bounded string."""
    totals = {}
    for label in attempts:
        totals[label] = totals.get(label, 0) + 1
    return "; ".join(
        f"{label} x{total}" if total > 1 else label for label, total in totals.items()
    )[:SUMMARY_LIMIT]


class Gemini:
    models = ("gemini-2.5-flash", "gemini-2.5-flash-lite")

    def __init__(self, keys: str, client=None, clock=time.monotonic):
        self.keys = list(
            dict.fromkeys(k.strip() for k in keys.split(",") if k.strip())
        )[:5]
        self.cooldowns = {}
        self.clock = clock
        self.client = client or httpx.AsyncClient(timeout=25)

    async def close(self):
        await self.client.aclose()

    def redact(self, text):
        """Remove configured keys from provider text before it reaches a log."""
        for key in self.keys:
            text = text.replace(key, REDACTED)
        return text

    def note(self, attempts, model, index, label, level=logging.WARNING, **fields):
        """Log one failed attempt and keep its key-free label for the summary."""
        attempts.append(label)
        details = " ".join(
            f"{name}={value}"
            for name, value in fields.items()
            if value not in (None, "")
        )
        log.log(
            level,
            "Gemini attempt failed model=%s key=#%d %s%s",
            model,
            index,
            label,
            f" {details}" if details else "",
        )

    def http_error(self, response):
        """One label: HTTP status, provider code and a redacted, capped message.

        Only the response body is read; the request, its headers and the key stay out.
        """
        label = f"http={response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return f"{label} body_not_json"
        error = body.get("error") if isinstance(body, dict) else None
        if not isinstance(error, dict):
            return label
        code = error.get("status")
        if isinstance(code, str) and code:
            label += f" {code}"
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            # repr keeps a multi-line provider message on a single log line.
            label += f" message={self.redact(message.strip())[:MESSAGE_LIMIT]!r}"
        return label

    @staticmethod
    def answer(payload):
        """Usable answer text, plus a key-free reason when there is none."""
        if not isinstance(payload, dict):
            return "", "payload_not_object"
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            feedback = payload.get("promptFeedback")
            blocked = (
                feedback.get("blockReason") if isinstance(feedback, dict) else None
            )
            if isinstance(blocked, str) and blocked:
                return "", f"blocked={blocked}"
            return "", "no_candidates"
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        finish = first.get("finishReason")
        tail = f" finish={finish}" if isinstance(finish, str) and finish else ""
        content = first.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            return "", f"no_parts{tail}"
        text = "".join(
            part.get("text", "")
            for part in parts
            if isinstance(part, dict) and not part.get("thought")
        )
        if not text.strip():
            return "", f"empty_text{tail}"
        return text, ""

    def parse(self, response):
        try:
            payload = response.json()
        except ValueError:
            return "", "body_not_json"
        return self.answer(payload)

    def timeout_setting(self):
        value = getattr(getattr(self.client, "timeout", None), "read", None)
        return f"{value:g}s" if isinstance(value, (int, float)) else ""

    async def request(self, model, key, prompt, json_mode):
        config = {
            "maxOutputTokens": 6000 if json_mode else 800,
            "thinkingConfig": {"thinkingBudget": 0},
        }
        if json_mode:
            config["responseMimeType"] = "application/json"
        return await self.client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": config,
            },
        )

    async def generate(self, prompt: str, *, json_mode=False):
        # Cooldowns are per project/key AND model: Lite can have a separate quota.
        attempts = []  # key-free labels only; prompts and answers are never stored
        chars = len(prompt)
        started = self.clock()
        for model in self.models:
            for index, key in enumerate(self.keys):
                if self.cooldowns.get((model, index), 0) > self.clock():
                    continue
                try:
                    response = await self.request(model, key, prompt, json_mode)
                    if response.status_code == 429:
                        self.cooldowns[model, index] = self.clock() + 60
                        self.note(
                            attempts,
                            model,
                            index,
                            f"{self.http_error(response)} cooldown=60s",
                            level=logging.INFO,
                            prompt_chars=chars,
                        )
                        continue
                    if response.is_error:
                        self.note(
                            attempts,
                            model,
                            index,
                            self.http_error(response),
                            prompt_chars=chars,
                        )
                        continue
                    text, reason = self.parse(response)
                    if reason:
                        self.note(
                            attempts,
                            model,
                            index,
                            f"http={response.status_code} {reason}",
                            prompt_chars=chars,
                        )
                        continue
                    if not json_mode:
                        return text
                    try:
                        return json.loads(text)
                    except ValueError as exc:
                        # Only the parse error and the answer length are logged.
                        self.note(
                            attempts,
                            model,
                            index,
                            f"invalid_json={type(exc).__name__}",
                            prompt_chars=chars,
                            answer_chars=len(text),
                        )
                except httpx.TimeoutException as exc:
                    self.note(
                        attempts,
                        model,
                        index,
                        f"timeout={type(exc).__name__}",
                        prompt_chars=chars,
                        limit=self.timeout_setting(),
                    )
                except httpx.HTTPError as exc:
                    self.note(
                        attempts,
                        model,
                        index,
                        f"network={type(exc).__name__}",
                        prompt_chars=chars,
                    )
                # A broken attempt must not kill the whole cascade.
                except Exception as exc:  # noqa: BLE001
                    self.note(
                        attempts,
                        model,
                        index,
                        f"unexpected={type(exc).__name__}",
                        prompt_chars=chars,
                    )
        summary = summarize(attempts) or (
            "all_keys_in_cooldown" if self.cooldowns else "no_keys_configured"
        )
        log.error(
            "Gemini exhausted models=%s attempts=%d json_mode=%s prompt_chars=%d "
            "elapsed=%.1fs cause=%s",
            ",".join(self.models),
            len(attempts),
            json_mode,
            chars,
            self.clock() - started,
            summary,
        )
        raise AIUnavailable(
            "ИИ временно недоступен: квоты или ошибка провайдера", summary=summary
        )
