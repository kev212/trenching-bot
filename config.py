import json
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field

CONFIG_DIR = Path(__file__).parent / "config"


class Settings(BaseSettings):
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    mimo_model: str = "mimo-v2.5-pro"
    gmgn_api_key: str = ""
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_filter_params() -> dict:
    path = CONFIG_DIR / "filter_params.json"
    with open(path) as f:
        return json.load(f)


def save_filter_params(params: dict):
    path = CONFIG_DIR / "filter_params.json"
    with open(path, "w") as f:
        json.dump(params, f, indent=2)


def load_adjustment_rules() -> dict:
    path = CONFIG_DIR / "adjustment_rules.json"
    with open(path) as f:
        return json.load(f)


settings = Settings()
