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
    top15_hold_pct: float = 0.0
    insider_ratio: float = 0.0
    rug_probability: float = 0.0
    funded_wallet_new_pct: float = 0.0
    top_holder_balance_sol: float = 0.0
    fee_collected: float = 0.0
    ath_price: float = 0.0
    ath_timestamp: int = 0
    drawdown_from_ath_pct: float = 0.0
    total_volume: float = 0.0
    dex_paid: bool = False
    is_wash_trading: bool = False
    created_at: Optional[datetime] = None
    creation_timestamp: int = 0
    open_timestamp: int = 0
    migrated_timestamp: int = 0
    raw_gmgn: dict = field(default_factory=dict)
    raw_dex: dict = field(default_factory=dict)
    
    # Social links (from GMGN)
    twitter_username: str = ""
    website_url: str = ""
    telegram_url: str = ""
    
    # Twitter data (from FxTwitter)
    twitter_followers: int = 0
    twitter_verified: bool = False
    twitter_description: str = ""
    recent_tweets: list = field(default_factory=list)
    
    # Influencer detection
    influencer_mentions: list = field(default_factory=list)
    organic_mentions: list = field(default_factory=list)
    has_elon_tweet: bool = False
    has_toly_tweet: bool = False
    has_community: bool = False

    # Website content
    website_text: str = ""

    # Social narrative scoring
    social_narrative_score: float = 0.0
    social_narrative_text: str = ""
    project_type: str = ""
    catalyst_match: bool = False
    catalyst_description: str = ""


@dataclass
class FeatureVector:
    funded_wallet_age: dict = field(default_factory=dict)
    min_market_cap: dict = field(default_factory=dict)
    max_market_cap: dict = field(default_factory=dict)
    insider_concentration: dict = field(default_factory=dict)
    fee_tier: dict = field(default_factory=dict)
    rug_probability: dict = field(default_factory=dict)
    holder_distribution: dict = field(default_factory=dict)
    token_age: dict = field(default_factory=dict)
    min_holders: dict = field(default_factory=dict)
    min_total_fee: dict = field(default_factory=dict)
    social_narrative: dict = field(default_factory=dict)
    ath_drawdown: dict = field(default_factory=dict)
    token_data: Optional[TokenData] = None

    def to_dict(self) -> dict:
        return {
            "funded_wallet_age": self.funded_wallet_age,
            "min_market_cap": self.min_market_cap,
            "max_market_cap": self.max_market_cap,
            "insider_concentration": self.insider_concentration,
            "fee_tier": self.fee_tier,
            "rug_probability": self.rug_probability,
            "holder_distribution": self.holder_distribution,
            "token_age": self.token_age,
            "min_holders": self.min_holders,
            "min_total_fee": self.min_total_fee,
            "social_narrative": self.social_narrative,
            "ath_drawdown": self.ath_drawdown,
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


@dataclass
class Position:
    token_address: str = ""
    token_symbol: str = ""
    side: str = "BUY"
    entry_tx_sig: str = ""
    entry_price: float = 0.0
    entry_amount_sol: float = 0.0
    entry_amount_token: float = 0.0
    entry_time: Optional[datetime] = None
    peak_price: float = 0.0
    current_amount_token: float = 0.0
    status: str = "OPEN"
    exit_tx_sig: str = ""
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    hold_seconds: int = 0
    exit_reason: str = ""
    filter_params_version: int = 0
    paper: bool = True
    id: int = 0


@dataclass
class Trade:
    position_id: int = 0
    side: str = "BUY"
    tx_signature: str = ""
    amount_in: float = 0.0
    amount_out: float = 0.0
    price: float = 0.0
    fee_sol: float = 0.0
    slippage_bps: int = 300
    priority_fee_sol: float = 0.0
    jito_tip_sol: float = 0.0
    slot: int = 0
    status: str = "PENDING"
    error: str = ""
    id: int = 0
    created_at: Optional[datetime] = None
