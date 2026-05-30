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

    async def send_message(self, text: str, parse_mode: str = None) -> bool:
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
                        logger.debug("Telegram message sent")
                        return True
                    else:
                        body = await resp.text()
                        logger.error(f"Telegram error {resp.status}: {body}")
                        return False

            except Exception as e:
                logger.error(f"Telegram send error: {e}")
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
