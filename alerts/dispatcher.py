import asyncio
import logging
import aiohttp
from config import settings

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"
MAX_MESSAGE_LENGTH = 4096


class TelegramDispatcher:
    def __init__(self):
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self._session = None
        self._send_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        # C3 fix: retry transient failures up to 3x with exponential backoff
        # (1s, 2s, 4s). Previously a single network blip silently dropped
        # the alert — particularly bad for buy/sell/exit signals.
        # C6 fix: default parse_mode = "Markdown" since all formatter output
        # is Markdown-formatted. Telegram falls back to plain text if the
        # message has no Markdown markers, so this is safe.
        last_err = None
        for attempt in range(3):
            async with self._send_lock:
                try:
                    session = await self._get_session()
                    url = f"{TELEGRAM_API.format(token=self.token)}/sendMessage"

                    payload = {
                        "chat_id": self.chat_id,
                        "text": text[:MAX_MESSAGE_LENGTH],
                    }
                    if parse_mode:
                        payload["parse_mode"] = parse_mode

                    async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            if attempt > 0:
                                logger.info(f"Telegram message sent on retry {attempt}")
                            else:
                                logger.debug("Telegram message sent")
                            return True
                        body = await resp.text()
                        last_err = f"HTTP {resp.status}: {body[:200]}"
                        logger.warning(
                            f"Telegram send failed (attempt {attempt+1}/3): {last_err}"
                        )
                except Exception as e:
                    last_err = str(e)
                    logger.warning(
                        f"Telegram send error (attempt {attempt+1}/3): {e}"
                    )

            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s

        logger.error(f"Telegram message DROPPED after 3 attempts: {last_err}")
        return False

    async def send_alert(self, text: str) -> bool:
        return await self.send_message(text)

    async def send_recap(self, text: str) -> bool:
        return await self.send_message(text)

    async def send_error(self, text: str) -> bool:
        return await self.send_message(f"🚨 ERROR\n\n{text}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


dispatcher = TelegramDispatcher()
