import json
import time
import logging
import asyncio
from openai import AsyncOpenAI
from config import settings

logger = logging.getLogger(__name__)

LLM_TIMEOUT = 30
BATCH_TIMEOUT = 90

# Anti-freeze tuning (June 2026 audit cycle 3)
# Worst-case per analyze_token call: (timeout + 2^attempt) × retries + sleep
# Before: 4 attempts × 60s + 7s = 247s per token — 2 workers could each
#   be stuck 4 min on a MiMo API outage.
# After:  2 attempts × 30s + 1s = 61s per token, capped further by the
#   semaphore wait_for (Fix #2) at +5s.
DEFAULT_RETRIES = 1
SEMAPHORE_WAIT_TIMEOUT = 5.0  # give up acquiring the LLM slot after 5s

# Circuit breaker tuning
CB_THRESHOLD = 5        # failures to open the circuit
CB_COOLDOWN_S = 60.0    # how long the circuit stays open


class CircuitBreaker:
    """Lightweight async circuit breaker.

    After `threshold` consecutive failures, the breaker opens for
    `cooldown` seconds. While open, all calls return immediately (the
    caller decides what `None`/empty means). First success after the
    cooldown closes the breaker.

    Defends against API outages: without it, an LLM outage causes
    `retries × timeout` per call = 184s of stall per worker. With it,
    each call returns in microseconds during the open period, so
    other work (filtering, social analysis without LLM, retries) can
    continue normally.
    """

    def __init__(self, threshold: int = CB_THRESHOLD, cooldown: float = CB_COOLDOWN_S):
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0
        # threading.Lock (not asyncio.Lock) so the breaker can be constructed
        # in sync contexts (tests, import-time). State is mutated only from
        # coroutines running in the same loop, so a regular lock is enough.
        import threading
        self._lock = threading.Lock()

    async def is_open(self) -> bool:
        with self._lock:
            if time.time() < self._open_until:
                return True
            # Cooldown expired — auto-close (next call will be a fresh attempt).
            if self._open_until > 0:
                logger.info("[LLM-CB] Cooldown elapsed, circuit auto-closed")
                self._open_until = 0.0
                self._failures = 0
            return False

    async def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold and self._open_until == 0.0:
                self._open_until = time.time() + self.cooldown
                logger.error(
                    f"[LLM-CB] OPEN for {self.cooldown}s after "
                    f"{self._failures} consecutive failures"
                )

    async def record_success(self):
        with self._lock:
            if self._failures > 0 or self._open_until > 0:
                logger.info(
                    f"[LLM-CB] CLOSED after success "
                    f"(prev failures={self._failures}, open={self._open_until > 0})"
                )
            self._failures = 0
            self._open_until = 0.0


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
        self._cb = CircuitBreaker()

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    async def analyze_token(
        self, system_prompt: str, user_prompt: str, temperature: float = 0.3,
        retries: int = DEFAULT_RETRIES,
    ) -> dict:
        start = time.time()
        # Circuit breaker check: return None immediately when open.
        if await self._cb.is_open():
            logger.debug("[LLM] circuit breaker open, returning None immediately")
            return None

        for attempt in range(retries + 1):
            try:
                # Fix #2: cap semaphore wait at 5s. If 4 LLM slots are all
                # busy with slow requests, don't queue — return None so the
                # worker can move on to the next token.
                try:
                    await asyncio.wait_for(
                        self._semaphore.acquire(), timeout=SEMAPHORE_WAIT_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"LLM semaphore saturated ({SEMAPHORE_WAIT_TIMEOUT}s timeout), "
                        f"skipping token to prevent worker stall"
                    )
                    return None
                try:
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
                finally:
                    self._semaphore.release()

                content = response.choices[0].message.content
                elapsed_ms = int((time.time() - start) * 1000)

                if not content or not content.strip():
                    if attempt < retries:
                        wait = 2 ** attempt
                        logger.warning(f"LLM empty content, retry {attempt+1}/{retries} (wait {wait}s)")
                        await asyncio.sleep(wait)
                        continue
                    logger.error(f"LLM empty content after {retries+1} attempts")
                    await self._cb.record_failure()
                    return None

                result = json.loads(content)
                result["_processing_time_ms"] = elapsed_ms
                await self._cb.record_success()
                return result

            except json.JSONDecodeError as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LLM invalid JSON, retry {attempt+1}/{retries} (wait {wait}s): {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM invalid JSON after {retries+1} attempts: {e}")
                await self._cb.record_failure()
                return None
            except asyncio.TimeoutError:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LLM timeout, retry {attempt+1}/{retries} (wait {wait}s)")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM timeout after {retries+1} attempts")
                await self._cb.record_failure()
                return None
            except Exception as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    logger.warning(f"LLM API error, retry {attempt+1}/{retries} (wait {wait}s): {e}")
                    await asyncio.sleep(wait)
                    continue
                logger.error(f"LLM API error after {retries+1} attempts: {e}")
                await self._cb.record_failure()
                return None

        return None

    async def analyze_batch(self, prompts: list):
        tasks = [self.analyze_token(sys, usr) for sys, usr in prompts]
        return await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=BATCH_TIMEOUT,
        )
