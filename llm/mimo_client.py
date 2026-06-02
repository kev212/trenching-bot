import json
import time
import logging
import asyncio
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger(__name__)


class MiMoClient:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
        )
        self.model = settings.mimo_model
        self._semaphore = asyncio.Semaphore(4)

    async def analyze_token(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.3
    ) -> dict:
        start = time.time()
        try:
            async with self._semaphore:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=temperature,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                )

            content = response.choices[0].message.content
            elapsed_ms = int((time.time() - start) * 1000)

            result = json.loads(content)
            result["_processing_time_ms"] = elapsed_ms
            return result

        except json.JSONDecodeError as e:
            logger.error(f"MiMo returned invalid JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"MiMo API error: {e}")
            return None

    async def analyze_batch(self, prompts: list):
        tasks = [self.analyze_token(sys, usr) for sys, usr in prompts]
        return await asyncio.gather(*tasks, return_exceptions=True)
