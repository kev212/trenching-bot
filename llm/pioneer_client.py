import json
import time
import logging
import asyncio
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger(__name__)

LLM_TIMEOUT = 60
BATCH_TIMEOUT = 90


class PioneerLLMClient:
    """OpenAI-compatible LLM client for Pioneer API (or any OpenAI endpoint).

    Uses AsyncOpenAI SDK. Point base_url to any OpenAI-compatible provider
    (Pioneer, OpenAI, etc.) via config.
    """

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=LLM_TIMEOUT,
        )
        self.model = settings.llm_model
        self._semaphore = asyncio.Semaphore(4)

    async def analyze_token(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.3, retries: int = 3
    ) -> dict:
        start = time.time()
        for attempt in range(retries + 1):
            try:
                async with self._semaphore:
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(
                            model=self.model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            temperature=temperature,
                            max_tokens=1024,
                            response_format={"type": "json_object"},
                        ),
                        timeout=LLM_TIMEOUT,
                    )

                content = response.choices[0].message.content
                elapsed_ms = int((time.time() - start) * 1000)

                if not content or not content.strip():
                    if attempt < retries:
                        wait = 2 ** attempt
                        logger.warning(f"LLM empty content, retry {attempt+1}/{retries} (wait {wait}s)")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"LLM empty content after {retries+1} attempts")
                    return None

                result = json.loads(content)
                result["_processing_time_ms"] = elapsed_ms
                return result

            except json.JSONDecodeError as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LLM invalid JSON, retry {attempt+1}/{retries} (wait {wait}s): {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM invalid JSON after {retries+1} attempts: {e}")
                return None
            except asyncio.TimeoutError:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LLM timeout, retry {attempt+1}/{retries} (wait {wait}s)")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM timeout after {retries+1} attempts")
                return None
            except Exception as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LLM API error, retry {attempt+1}/{retries} (wait {wait}s): {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM API error after {retries+1} attempts: {e}")
                return None

        return None

    async def analyze_batch(self, prompts: list):
        tasks = [self.analyze_token(sys, usr) for sys, usr in prompts]
        return await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=BATCH_TIMEOUT,
        )
