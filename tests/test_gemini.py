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
                if "/gemini-2.5-flash:" in request.url.path
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
