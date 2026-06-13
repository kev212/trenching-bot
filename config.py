import json
import asyncio
from pathlib import Path
import os
from pydantic_settings import BaseSettings
from pydantic import Field

CONFIG_DIR = Path(__file__).parent / "config"


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    llm_api_key: str = ""
    llm_base_url: str = "https://api.xiaomimimo.com/v1"
    llm_model: str = "mimo-v2.5"

    def model_post_init(self, __context):
        if not self.llm_api_key:
            self.llm_api_key = os.environ.get("MIMO_API_KEY", "")
    gmgn_api_key: str = ""
    http_proxy: str = ""
    log_level: str = "INFO"
    db_path: str = "trenching.db"
    max_queue_size: int = 1000
    min_workers: int = 2
    max_workers: int = 5
    worker_scale_up_threshold: int = 50
    worker_scale_down_threshold: int = 10
    price_check_interval: int = 300
    win_target_multiplier: float = 1.3
    win_time_limit_seconds: int = 1800

    paper_mode: bool = True
    helius_api_key: str = ""
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    fernet_key: str = ""
    private_key_encrypted: str = ""
    wallet_private_key: str = ""
    wallet_pubkey: str = ""
    paper_starting_balance_sol: float = 10.0
    confidence_auto_execute: float = 0.60  # DEPRECATED 2026-06-14: APE threshold (60) is now the gate; conf_auto_execute unused
    enable_trenches_poller: bool = True

    # June 2026 audit cycle 3 — anti-freeze tuning
    jupiter_retry_total_timeout_s: float = 8.0
    retry_scheduler_lock_budget: int = 50  # max addresses processed per lock hold

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_filter_params() -> dict:
    path = CONFIG_DIR / "filter_params.json"
    with open(path) as f:
        return json.load(f)


def save_filter_params(params: dict):
    path = CONFIG_DIR / "filter_params.json"
    with open(path, "w") as f:
        json.dump(params, f, indent=2)


async def load_filter_params_async() -> dict:
    """Async version of load_filter_params — uses thread to avoid blocking event loop."""
    def _load():
        path = CONFIG_DIR / "filter_params.json"
        with open(path) as f:
            return json.load(f)
    return await asyncio.to_thread(_load)


async def save_filter_params_async(params: dict):
    """Async version of save_filter_params — uses thread to avoid blocking event loop."""
    def _save():
        path = CONFIG_DIR / "filter_params.json"
        with open(path, "w") as f:
            json.dump(params, f, indent=2)
    await asyncio.to_thread(_save)


def load_adjustment_rules() -> dict:
    path = CONFIG_DIR / "adjustment_rules.json"
    with open(path) as f:
        return json.load(f)


def load_trading_config() -> dict:
    path = CONFIG_DIR / "trading.json"
    with open(path) as f:
        return json.load(f)


def load_risk_rules() -> dict:
    path = CONFIG_DIR / "risk_rules.json"
    with open(path) as f:
        return json.load(f)


settings = Settings()

# Position monitor defaults (can be overridden by trading.json)
POSITION_MONITOR_DEFAULTS = {
    'stop_loss_pct': 50,
    'tp1_sell_pct': 80,
    'tp2_multiplier': 2.0,
    'trailing_stop_pct': 20,
    'time_limit_seconds': 0,
    'min_hold_seconds': 30,
    'extreme_tp_mult': 2.0,
}
