import asyncio
import json
import logging
import websockets
from config import settings

logger = logging.getLogger(__name__)

WS_URL = "wss://pumpportal.fun/api/data"


async def ws_listener(queue: asyncio.Queue, state):
    logger.info("WS Listener starting...")
    url = WS_URL

    while True:
        try:
            logger.info("Connecting to PumpPortal WebSocket...")
            async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
                logger.info("PumpPortal WebSocket connected")

                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                logger.info("Subscribed to new tokens and migrations")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        token = _parse_token_event(data)
                        if token and not await state.is_duplicate(token["address"]):
                            priority = 0 if token.get("event_type") == "new_token" else 1
                            await queue.put((priority, token))
                            logger.debug(f"Queued: {token['symbol']} ({token['address'][:8]}...)")
                    except Exception as e:
                        logger.error(f"Error parsing WS message: {e}")

        except websockets.ConnectionClosed:
            logger.warning("PumpPortal WS disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"PumpPortal WS error: {e}, reconnecting in 10s...")
            await asyncio.sleep(10)


def _parse_token_event(data: dict):
    if not isinstance(data, dict):
        return None

    mint = data.get("mint") or data.get("tokenAddress")
    if not mint:
        return None

    return {
        "address": mint,
        "name": data.get("name", ""),
        "symbol": data.get("symbol", ""),
        "event_type": "new_token" if "mint" in data else "migration",
        "source": "pumpportal",
        "initial_buy": data.get("initialBuy", 0),
        "market_cap": data.get("marketCapSol", 0) * 150,
        "trader": data.get("traderPublicKey", ""),
        "raw": data,
    }
