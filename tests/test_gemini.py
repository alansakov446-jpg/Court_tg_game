import json
import unittest

import httpx

from ai.gemini import AIUnavailable, Gemini


def success(text="hello"):
    return httpx.Response(
        200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]}
    )


class CascadeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_flash_keys_before_lite_and_cooldown(self):
        calls = []

        def handler(request):
            calls.append((request.url.path, request.headers["x-goog-api-key"]))
            return (
                httpx.Response(429)
                if f"/{Gemini.models[0]}:" in request.url.path
                else success()
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        now = [100.0]
        ai = Gemini("a,b,c,d,e", client=client, clock=lambda: now[0])
        self.assertEqual(await ai.generate("test"), "hello")
        self.assertEqual([key for _, key in calls], ["a", "b", "c", "d", "e", "a"])
        calls.clear()
        await ai.generate("test")
        self.assertEqual(len(calls), 1)
        now[0] = 161
        calls.clear()
        await ai.generate("test")
        self.assertEqual(len(calls), 6)
        await ai.close()

    async def test_server_error_cools_the_whole_model(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if f"/{Gemini.models[0]}:" in request.url.path:
                return httpx.Response(
                    503,
                    json={
                        "error": {
                            "status": "UNAVAILABLE",
                            "message": "high demand",
                        }
                    },
                )
            return success()

        ai = Gemini(
            "a,b,c", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(await ai.generate("test"), "hello")
        # One 503 is enough: the other flash keys are skipped, lite answers.
        self.assertEqual(len(calls), 2)
        self.assertIn(f"/{Gemini.models[0]}:", calls[0])
        self.assertIn(f"/{Gemini.models[1]}:", calls[1])
        await ai.close()

    async def test_timeout_cools_the_whole_model(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if f"/{Gemini.models[0]}:" in request.url.path:
                raise httpx.ReadTimeout("read timed out")
            return success()

        ai = Gemini(
            "a,b", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(await ai.generate("test"), "hello")
        # A hanging model must not eat the budget key by key.
        self.assertEqual(len(calls), 2)
        self.assertIn(f"/{Gemini.models[1]}:", calls[1])
        await ai.close()

    async def test_thinking_budget_rejected_model_is_retried_slim(self):
        calls = []

        def handler(request):
            calls.append(request)
            config = json.loads(request.content)["generationConfig"]
            if "thinkingConfig" in config:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "status": "INVALID_ARGUMENT",
                            "message": "Request contains an invalid argument.",
                        }
                    },
                )
            return success()

        ai = Gemini(
            "a", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(await ai.generate("test"), "hello")
        self.assertEqual(len(calls), 2)  # full config 400 -> slim config answers
        self.assertIn(
            "thinkingConfig",
            json.loads(calls[0].content)["generationConfig"],
        )
        self.assertNotIn(
            "thinkingConfig",
            json.loads(calls[1].content)["generationConfig"],
        )
        await ai.close()

    async def test_slim_config_is_remembered_per_model(self):
        calls = []

        def handler(request):
            calls.append(request)
            config = json.loads(request.content)["generationConfig"]
            if "thinkingConfig" in config:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "status": "INVALID_ARGUMENT",
                            "message": "Request contains an invalid argument.",
                        }
                    },
                )
            return success()

        ai = Gemini(
            "a", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(await ai.generate("test"), "hello")
        self.assertEqual(await ai.generate("test"), "hello")
        # Second call goes straight to the slim config: 2 requests then 1.
        self.assertEqual(len(calls), 3)
        self.assertNotIn(
            "thinkingConfig",
            json.loads(calls[2].content)["generationConfig"],
        )
        await ai.close()

    async def test_exhaustion_and_no_key_leak(self):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(429))
        )
        ai = Gemini("secret", client=client)
        with self.assertRaises(AIUnavailable) as cm:
            await ai.generate("test")
        self.assertNotIn("secret", str(cm.exception))
        await ai.close()

    async def test_json_and_malformed_response_fallback(self):
        calls = []

        def handler(request):
            calls.append(request)
            return success("not json" if len(calls) == 1 else '{"vote":"innocent"}')

        ai = Gemini(
            "a,b", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(
            await ai.generate("test", json_mode=True), {"vote": "innocent"}
        )
        self.assertEqual(len(calls), 2)
        await ai.close()

    async def test_empty_keys(self):
        ai = Gemini("")
        with self.assertRaises(AIUnavailable):
            await ai.generate("test")
        await ai.close()


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """A failure must be explainable from the logs, without leaking keys or content."""

    def client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_provider_error_is_logged_and_key_is_redacted(self):
        def handler(_):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "API key not valid: secret",
                    }
                },
            )

        ai = Gemini("secret", client=self.client(handler))
        with (
            self.assertLogs("court.ai", level="WARNING") as logs,
            self.assertRaises(AIUnavailable) as cm,
        ):
            await ai.generate("test")
        output = "\n".join(logs.output)
        self.assertIn("http=400 INVALID_ARGUMENT", output)
        self.assertIn(f"model={Gemini.models[0]}", output)
        self.assertIn("[redacted]", output)
        self.assertNotIn("secret", output)
        self.assertIn("http=400 INVALID_ARGUMENT", cm.exception.summary)
        self.assertNotIn("secret", cm.exception.summary)
        await ai.close()

    async def test_timeout_is_logged_with_model_and_limit(self):
        def handler(_):
            raise httpx.ReadTimeout("read timed out")

        ai = Gemini("a", client=self.client(handler))
        with (
            self.assertLogs("court.ai", level="WARNING") as logs,
            self.assertRaises(AIUnavailable) as cm,
        ):
            await ai.generate("test", json_mode=True)
        output = "\n".join(logs.output)
        self.assertIn("timeout=ReadTimeout", output)
        self.assertIn("limit=5s", output)
        self.assertIn(Gemini.models[1], output)
        self.assertIn("json_mode=True", output)
        self.assertEqual(cm.exception.summary, "timeout=ReadTimeout x2")
        await ai.close()

    async def test_invalid_json_logs_length_but_not_the_answer(self):
        ai = Gemini("a", client=self.client(lambda _: success("not json")))
        with (
            self.assertLogs("court.ai", level="WARNING") as logs,
            self.assertRaises(AIUnavailable),
        ):
            await ai.generate("test", json_mode=True)
        output = "\n".join(logs.output)
        self.assertIn("invalid_json=JSONDecodeError", output)
        self.assertIn("answer_chars=8", output)
        self.assertNotIn("not json", output)
        await ai.close()

    async def test_blocked_and_truncated_answers_are_named(self):
        answers = [
            httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}),
            httpx.Response(
                200,
                json={"candidates": [{"finishReason": "MAX_TOKENS", "content": {}}]},
            ),
        ]
        calls = []

        def handler(_):
            calls.append(1)
            return answers[0] if len(calls) == 1 else answers[1]

        ai = Gemini("a", client=self.client(handler))
        with (
            self.assertLogs("court.ai", level="WARNING") as logs,
            self.assertRaises(AIUnavailable) as cm,
        ):
            await ai.generate("test")
        output = "\n".join(logs.output)
        self.assertIn("blocked=SAFETY", output)
        self.assertIn("no_parts finish=MAX_TOKENS", output)
        self.assertIn("blocked=SAFETY", cm.exception.summary)
        await ai.close()

    async def test_cooldown_exhaustion_reports_cause_without_requests(self):
        calls = []

        def handler(_):
            calls.append(1)
            return httpx.Response(429)

        ai = Gemini("a,b", client=self.client(handler))
        with self.assertRaises(AIUnavailable):
            await ai.generate("test")
        self.assertEqual(len(calls), 4)
        calls.clear()
        with (
            self.assertLogs("court.ai", level="ERROR") as logs,
            self.assertRaises(AIUnavailable) as cm,
        ):
            await ai.generate("test")
        self.assertEqual(calls, [])  # cooldowns prevent pointless retries
        self.assertEqual(cm.exception.summary, "all_keys_in_cooldown")
        self.assertIn("attempts=0", "\n".join(logs.output))
        await ai.close()
