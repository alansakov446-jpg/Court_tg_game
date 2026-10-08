import json
import time

import httpx


class AIUnavailable(RuntimeError):
    pass


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

    async def generate(self, prompt: str, *, json_mode=False):
        # Cooldowns are per project/key AND model: Lite can have a separate quota.
        for model in self.models:
            for index, key in enumerate(self.keys):
                if self.cooldowns.get((model, index), 0) > self.clock():
                    continue
                try:
                    config = {
                        "maxOutputTokens": 6000 if json_mode else 800,
                        "thinkingConfig": {"thinkingBudget": 0},
                    }
                    if json_mode:
                        config["responseMimeType"] = "application/json"
                    response = await self.client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        headers={"x-goog-api-key": key},
                        json={
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": config,
                        },
                    )
                    if response.status_code == 429:
                        self.cooldowns[model, index] = self.clock() + 60
                        continue
                    if response.is_error:
                        continue
                    parts = response.json()["candidates"][0]["content"]["parts"]
                    text = "".join(
                        p.get("text", "") for p in parts if not p.get("thought")
                    )
                    if text.strip():
                        return json.loads(text) if json_mode else text
                except (httpx.HTTPError, KeyError, IndexError, ValueError):
                    continue
        raise AIUnavailable("ИИ временно недоступен: квоты или ошибка провайдера")
