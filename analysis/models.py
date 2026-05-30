from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    APE = "APE"
    WATCH = "WATCH"
    SKIP = "SKIP"


class CallStatus(str, Enum):
    PENDING = "PENDING"
    WIN = "WIN"
    LOSS = "LOSS"


@dataclass
class TokenData:
    address: str
    name: str = ""
    symbol: str = ""
    chain: str = "solana"
    market_cap: float = 0.0
    volume_1h: float = 0.0
    liquidity: float = 0.0
    holders_count: int = 0
    top10_hold_pct: float = 0.0
    insider_ratio: float = 0.0
    rug_probability: float = 0.0
    funded_wallet_new_pct: float = 0.0
    top_holder_balance_sol: float = 0.0
    fee_collected: float = 0.0
    total_volume: float = 0.0
    dex_paid: bool = False
    is_wash_trading: bool = False
    social_narrative_score: float = 0.0
    social_narrative_text: str = ""
    created_at: Optional[datetime] = None
    raw_gmgn: dict = field(default_factory=dict)
    raw_dex: dict = field(default_factory=dict)


@dataclass
class FeatureVector:
    funded_wallet_age: dict = field(default_factory=dict)
    top_holder_balance: dict = field(default_factory=dict)
    min_market_cap: dict = field(default_factory=dict)
    max_market_cap: dict = field(default_factory=dict)
    bundle_detection: dict = field(default_factory=dict)
    mc_fee_ratio: dict = field(default_factory=dict)
    rug_probability: dict = field(default_factory=dict)
    holder_distribution: dict = field(default_factory=dict)
    token_age: dict = field(default_factory=dict)
    min_holders: dict = field(default_factory=dict)
    min_total_fee: dict = field(default_factory=dict)
    wash_trading: dict = field(default_factory=dict)
    social_narrative: dict = field(default_factory=dict)
    token_data: Optional[TokenData] = None

    def to_dict(self) -> dict:
        return {
            "funded_wallet_age": self.funded_wallet_age,
            "top_holder_balance": self.top_holder_balance,
            "min_market_cap": self.min_market_cap,
            "max_market_cap": self.max_market_cap,
            "bundle_detection": self.bundle_detection,
            "mc_fee_ratio": self.mc_fee_ratio,
            "rug_probability": self.rug_probability,
            "holder_distribution": self.holder_distribution,
            "token_age": self.token_age,
            "min_holders": self.min_holders,
            "min_total_fee": self.min_total_fee,
            "wash_trading": self.wash_trading,
            "social_narrative": self.social_narrative,
        }


@dataclass
class LLMDecision:
    score: int = 0
    verdict: Verdict = Verdict.SKIP
    reasoning: str = ""
    confidence: float = 0.0
    key_factors: list = field(default_factory=list)
    processing_time_ms: int = 0


@dataclass
class CallRecord:
    id: Optional[int] = None
    token_address: str = ""
    token_name: str = ""
    token_symbol: str = ""
    call_time: Optional[datetime] = None
    entry_price: float = 0.0
    market_cap_at_call: float = 0.0
    volume_1h: float = 0.0
    liquidity: float = 0.0
    holders_count: int = 0
    llm_score: int = 0
    llm_verdict: str = ""
    llm_reasoning: str = ""
    llm_confidence: float = 0.0
    llm_key_factors: str = ""
    filter_params_version: int = 1
    feature_vector: str = ""
    status: CallStatus = CallStatus.PENDING
    max_gain: float = 1.0
    max_gain_time: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


@dataclass
class PriceSnapshot:
    call_id: int = 0
    price: float = 0.0
    gain: float = 1.0
    snapshot_time: Optional[datetime] = None


@dataclass
class FilterAdjustment:
    filter_name: str = ""
    param_name: str = ""
    old_value: float = 0.0
    new_value: float = 0.0
    reason: str = ""
    confidence: float = 0.0
