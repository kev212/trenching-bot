import aiosqlite
import logging

from datetime import datetime, timedelta, timezone
from typing import Optional
from analysis.models import CallRecord, PriceSnapshot, CallStatus

logger = logging.getLogger("main")

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL UNIQUE,
    token_name TEXT,
    token_symbol TEXT,
    call_time TIMESTAMP NOT NULL,
    entry_price REAL NOT NULL,
    market_cap_at_call REAL,
    volume_1h REAL,
    liquidity REAL,
    holders_count INTEGER,
    llm_score INTEGER,
    llm_verdict TEXT,
    llm_reasoning TEXT,
    llm_confidence REAL,
    llm_key_factors TEXT,
    filter_params_version INTEGER,
    feature_vector TEXT,
    status TEXT DEFAULT 'PENDING',
    max_gain REAL DEFAULT 1.0,
    max_gain_time TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER REFERENCES calls(id),
    snapshot_time TIMESTAMP NOT NULL,
    price REAL NOT NULL,
    gain REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS filter_params_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    params_json TEXT NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deactivated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS filter_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filter_name TEXT NOT NULL,
    param_name TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    reason TEXT,
    confidence REAL,
    applied_at TIMESTAMP,
    win_rate_before REAL,
    win_rate_after REAL,
    resulting_version INTEGER,
    reverted BOOLEAN DEFAULT FALSE,
    reverted_at TIMESTAMP,
    revert_reason TEXT
);

CREATE TABLE IF NOT EXISTS llm_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER REFERENCES calls(id),
    score INTEGER,
    verdict TEXT,
    reasoning TEXT,
    confidence REAL,
    key_factors TEXT,
    processing_time_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL UNIQUE,
    total_calls INTEGER,
    wins INTEGER,
    losses INTEGER,
    pending INTEGER,
    win_rate REAL,
    avg_gain REAL,
    best_token TEXT,
    best_gain REAL,
    filter_params_version INTEGER,
    llm_summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TIMESTAMP NOT NULL,
    period_end TIMESTAMP NOT NULL,
    total_calls INTEGER,
    wins INTEGER,
    losses INTEGER,
    pending INTEGER,
    win_rate REAL,
    avg_gain REAL,
    best_token TEXT,
    best_gain REAL,
    llm_loss_analysis TEXT,
    sent_to_telegram BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_call_time ON calls(call_time);
CREATE INDEX IF NOT EXISTS idx_calls_token_address ON calls(token_address);
CREATE INDEX IF NOT EXISTS idx_snapshots_call_id ON price_snapshots(call_id);
CREATE INDEX IF NOT EXISTS idx_adjustments_applied ON filter_adjustments(applied_at);

-- Phase E2-Alert: data persistence for loss learning
CREATE TABLE IF NOT EXISTS filter_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    token_name TEXT,
    token_symbol TEXT,
    market_cap REAL,
    holders_count INTEGER,
    age_minutes REAL,
    filter_results TEXT NOT NULL,  -- JSON: {filter_name: {passed, value, threshold}}
    passed BOOLEAN NOT NULL,
    failed_filters TEXT,           -- JSON: list of failed filter names
    was_retried BOOLEAN DEFAULT FALSE,
    retry_count INTEGER DEFAULT 0,
    filter_params_version INTEGER,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS skip_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    token_name TEXT,
    token_symbol TEXT,
    llm_score INTEGER,
    llm_reasoning TEXT,
    llm_key_factors TEXT,          -- JSON: list of strings
    market_cap REAL,
    holders_count INTEGER,
    age_minutes REAL,
    top15_pct REAL,
    social_score REAL,
    feature_vector TEXT,           -- JSON: full fv
    skipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS loss_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER REFERENCES calls(id),
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    root_cause TEXT,
    wrong_filter TEXT,
    suggestion TEXT,
    pattern TEXT,
    confidence REAL,
    llm_raw TEXT,                  -- Full LLM #3 response
    max_gain REAL,
    elapsed_seconds REAL,
    analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_filter_outcomes_passed ON filter_outcomes(passed);
CREATE INDEX IF NOT EXISTS idx_filter_outcomes_processed_at ON filter_outcomes(processed_at);
CREATE INDEX IF NOT EXISTS idx_filter_outcomes_token ON filter_outcomes(token_address);
CREATE INDEX IF NOT EXISTS idx_skip_decisions_skipped_at ON skip_decisions(skipped_at);
CREATE INDEX IF NOT EXISTS idx_skip_decisions_llm_score ON skip_decisions(llm_score);
CREATE INDEX IF NOT EXISTS idx_loss_analyses_call_id ON loss_analyses(call_id);
CREATE INDEX IF NOT EXISTS idx_loss_analyses_analyzed_at ON loss_analyses(analyzed_at);

-- Trading bot tables (Phase 1: paper mode)
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL UNIQUE,
    token_symbol TEXT,
    side TEXT DEFAULT 'LONG',
    entry_tx_sig TEXT,
    entry_price REAL NOT NULL,
    entry_amount_sol REAL NOT NULL,
    entry_amount_token REAL NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    peak_price REAL DEFAULT 0.0,
    current_amount_token REAL NOT NULL,
    total_sold_sol REAL DEFAULT 0.0,
    status TEXT DEFAULT 'OPEN',
    exit_tx_sig TEXT,
    exit_price REAL,
    exit_time TIMESTAMP,
    pnl_sol REAL,
    pnl_pct REAL,
    hold_seconds INTEGER,
    exit_reason TEXT,
    filter_params_version INTEGER,
    paper INTEGER DEFAULT 1,
    raw_gmgn_json TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    side TEXT NOT NULL,
    tx_signature TEXT,
    amount_in REAL NOT NULL,
    amount_out REAL NOT NULL,
    price REAL NOT NULL,
    fee_sol REAL DEFAULT 0.0,
    slippage_bps INTEGER DEFAULT 0,
    priority_fee_sol REAL DEFAULT 0.0,
    jito_tip_sol REAL DEFAULT 0.0,
    slot INTEGER,
    status TEXT DEFAULT 'PENDING',
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallet_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sol_balance REAL NOT NULL,
    token_count INTEGER DEFAULT 0,
    total_value_sol REAL,
    unrealized_pnl_sol REAL DEFAULT 0.0,
    paper INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    token_address TEXT,
    details TEXT,
    paper INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(token_address);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);
CREATE INDEX IF NOT EXISTS idx_risk_events_type ON risk_events(event_type);
"""


class Database:
    def __init__(self, db_path: str = "trenching.db"):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def init(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.executescript(DB_SCHEMA)
        await self._migrate()
        await self.db.commit()

    async def _migrate(self):
        """Lightweight ALTER TABLE migrations for existing DBs."""
        migrations = [
            ("positions", "raw_gmgn_json", "TEXT DEFAULT ''"),
            ("positions", "total_sold_sol", "REAL DEFAULT 0.0"),
            ("filter_adjustments", "resulting_version", "INTEGER"),
        ]
        for table, column, typedef in migrations:
            try:
                await self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {typedef}"
                )
                logger.info(f"[DB-MIGRATE] Added {table}.{column}")
            except Exception:
                pass

    async def close(self):
        if self.db:
            await self.db.close()

    async def commit(self):
        if self.db:
            await self.db.commit()

    async def save_call(self, call: CallRecord) -> int:
        cursor = await self.db.execute(
            """INSERT OR REPLACE INTO calls
            (token_address, token_name, token_symbol, call_time, entry_price,
             market_cap_at_call, volume_1h, liquidity, holders_count,
             llm_score, llm_verdict, llm_reasoning, llm_confidence,
             llm_key_factors, filter_params_version, feature_vector, status,
             max_gain, max_gain_time, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                call.token_address, call.token_name, call.token_symbol,
                call.call_time.isoformat() if call.call_time else None,
                call.entry_price, call.market_cap_at_call, call.volume_1h,
                call.liquidity, call.holders_count, call.llm_score,
                call.llm_verdict, call.llm_reasoning, call.llm_confidence,
                call.llm_key_factors, call.filter_params_version,
                call.feature_vector, call.status.value,
                call.max_gain,
                call.max_gain_time.isoformat() if call.max_gain_time else None,
                call.completed_at.isoformat() if call.completed_at else None,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_call_by_address(self, address: str) -> Optional[CallRecord]:
        cursor = await self.db.execute(
            "SELECT * FROM calls WHERE token_address = ?", (address,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_call(row)

    async def get_active_calls(self) -> list[CallRecord]:
        cursor = await self.db.execute(
            "SELECT * FROM calls WHERE status = 'PENDING' ORDER BY call_time DESC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_call(r) for r in rows]

    async def get_calls_in_range(
        self, start: datetime, end: datetime
    ) -> list[CallRecord]:
        cursor = await self.db.execute(
            "SELECT * FROM calls WHERE call_time BETWEEN ? AND ? ORDER BY call_time",
            (start.isoformat(), end.isoformat()),
        )
        rows = await cursor.fetchall()
        return [self._row_to_call(r) for r in rows]

    async def update_call_status(
        self, call_id: int, status: CallStatus, max_gain: float = 1.0
    ):
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """UPDATE calls SET status = ?, max_gain = MAX(max_gain, ?),
             completed_at = CASE WHEN ? != 'PENDING' THEN ? ELSE completed_at END,
             max_gain_time = CASE WHEN ? > max_gain THEN ? ELSE max_gain_time END
             WHERE id = ?""",
            (status.value, max_gain, status.value, now, max_gain, now, call_id),
        )
        await self.db.commit()

    async def save_price_snapshot(self, snapshot: PriceSnapshot):
        await self.db.execute(
            "INSERT INTO price_snapshots (call_id, snapshot_time, price, gain) VALUES (?, ?, ?, ?)",
            (
                snapshot.call_id,
                snapshot.snapshot_time.isoformat() if snapshot.snapshot_time else datetime.now(timezone.utc).isoformat(),
                snapshot.price,
                snapshot.gain,
            ),
        )
        await self.db.commit()

    async def save_llm_decision(
        self, call_id: int, score: int, verdict: str, reasoning: str,
        confidence: float, key_factors: list, processing_time_ms: int
    ):
        import json
        await self.db.execute(
            """INSERT INTO llm_decisions
            (call_id, score, verdict, reasoning, confidence, key_factors, processing_time_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (call_id, score, verdict, reasoning, confidence, json.dumps(key_factors), processing_time_ms),
        )
        await self.db.commit()

    async def save_adjustment(
        self, filter_name: str, param_name: str, old_value: float,
        new_value: float, reason: str, confidence: float, win_rate_before: float,
        resulting_version: int = 0,
    ):
        await self.db.execute(
            """INSERT INTO filter_adjustments
            (filter_name, param_name, old_value, new_value, reason, confidence,
             applied_at, win_rate_before, resulting_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (filter_name, param_name, old_value, new_value, reason, confidence,
             datetime.now(timezone.utc).isoformat(), win_rate_before, resulting_version),
        )
        await self.db.commit()

    async def get_adjustments_since(self, since: datetime):
        cursor = await self.db.execute(
            "SELECT * FROM filter_adjustments WHERE applied_at >= ? AND reverted = FALSE",
            (since.isoformat(),),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def revert_adjustment(self, adjustment_id: int, reason: str):
        await self.db.execute(
            """UPDATE filter_adjustments SET reverted = TRUE, reverted_at = ?, revert_reason = ?
             WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), reason, adjustment_id),
        )
        await self.db.commit()

    async def get_win_rate_since(self, since: datetime) -> tuple[int, int, float]:
        cursor = await self.db.execute(
            """SELECT COUNT(*) as total,
             SUM(CASE WHEN status = 'WIN' THEN 1 ELSE 0 END) as wins
             FROM calls WHERE call_time >= ? AND status IN ('WIN', 'LOSS')""",
            (since.isoformat(),),
        )
        row = await cursor.fetchone()
        total = row["total"] or 0
        wins = row["wins"] or 0
        wr = (wins / total * 100) if total > 0 else 0.0
        return total, wins, wr

    async def get_win_rate_for_version(self, version: int) -> tuple[int, int, float]:
        """Win rate for a specific filter_params_version cohort.

        Replaces the (broken) global `get_win_rate_since` usage in the
        revert monitor: a single adjustment's effect should be measured
        against calls made UNDER the new params, not against a global
        snapshot that includes old-param calls.
        """
        cursor = await self.db.execute(
            """SELECT COUNT(*) as total,
             SUM(CASE WHEN status = 'WIN' THEN 1 ELSE 0 END) as wins
             FROM calls WHERE filter_params_version = ? AND status IN ('WIN', 'LOSS')""",
            (version,),
        )
        row = await cursor.fetchone()
        total = row["total"] or 0
        wins = row["wins"] or 0
        wr = (wins / total * 100) if total > 0 else 0.0
        return total, wins, wr

    async def save_daily_stats(self, date: str, stats: dict):
        await self.db.execute(
            """INSERT OR REPLACE INTO daily_stats
            (date, total_calls, wins, losses, pending, win_rate, avg_gain, best_token, best_gain)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date, stats["total"], stats["wins"], stats["losses"],
                stats["pending"], stats["win_rate"], stats["avg_gain"],
                stats.get("best_token"), stats.get("best_gain"),
            ),
        )
        await self.db.commit()

    async def save_recap(self, recap: dict):
        await self.db.execute(
            """INSERT INTO recaps
            (period_start, period_end, total_calls, wins, losses, pending, win_rate, avg_gain, best_token, best_gain, llm_loss_analysis)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                recap["period_start"], recap["period_end"], recap["total"],
                recap["wins"], recap["losses"], recap["pending"],
                recap["win_rate"], recap["avg_gain"], recap.get("best_token"),
                recap.get("best_gain"), recap.get("llm_loss_analysis"),
            ),
        )
        await self.db.commit()

    # Phase E2-Alert: filter_outcomes (per-token audit trail)

    async def save_filter_outcome(
        self,
        token_address: str,
        token_name: str,
        token_symbol: str,
        market_cap: float,
        holders_count: int,
        age_minutes: float,
        filter_results: dict,
        passed: bool,
        failed_filters: list,
        was_retried: bool = False,
        retry_count: int = 0,
        filter_params_version: int = 1,
    ):
        """Save outcome of hard-gate filter check for every token seen (pass or fail)."""
        import json as _json
        await self.db.execute(
            """INSERT INTO filter_outcomes
            (token_address, token_name, token_symbol, market_cap, holders_count, age_minutes,
             filter_results, passed, failed_filters, was_retried, retry_count, filter_params_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_address, token_name, token_symbol, market_cap, holders_count, age_minutes,
                _json.dumps(filter_results), passed, _json.dumps(failed_filters),
                was_retried, retry_count, filter_params_version,
            ),
        )
        await self.db.commit()

    async def save_skip_decision(
        self,
        token_address: str,
        token_name: str,
        token_symbol: str,
        llm_score: int,
        llm_reasoning: str,
        llm_key_factors: list,
        market_cap: float,
        holders_count: int,
        age_minutes: float,
        top15_pct: float,
        social_score: float,
        feature_vector: dict,
    ):
        """Save every LLM #2 SKIP verdict for retro-tuning analysis."""
        import json as _json
        await self.db.execute(
            """INSERT INTO skip_decisions
            (token_address, token_name, token_symbol, llm_score, llm_reasoning, llm_key_factors,
             market_cap, holders_count, age_minutes, top15_pct, social_score, feature_vector)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_address, token_name, token_symbol, llm_score, llm_reasoning,
                _json.dumps(llm_key_factors), market_cap, holders_count, age_minutes,
                top15_pct, social_score, _json.dumps(feature_vector),
            ),
        )
        await self.db.commit()

    async def save_loss_analysis(
        self,
        call_id: int,
        token_address: str,
        token_symbol: str,
        root_cause: str,
        wrong_filter: str,
        suggestion: str,
        pattern: str,
        confidence: float,
        llm_raw: str,
        max_gain: float,
        elapsed_seconds: float,
    ):
        """Save LLM #3 root-cause analysis for a LOSS call."""
        await self.db.execute(
            """INSERT INTO loss_analyses
            (call_id, token_address, token_symbol, root_cause, wrong_filter, suggestion,
             pattern, confidence, llm_raw, max_gain, elapsed_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                call_id, token_address, token_symbol, root_cause, wrong_filter, suggestion,
                pattern, confidence, llm_raw, max_gain, elapsed_seconds,
            ),
        )
        await self.db.commit()

    async def get_filter_performance_since(self, since: datetime) -> dict:
        """Compute per-filter pass/fail rates from filter_outcomes.

        Returns:
            {filter_name: {"total": N, "failed": N, "fail_rate": 0.0-1.0, "top_failure_reasons": [...]}}
        """
        import json as _json
        # Normalize `since` to SQLite-compatible format (YYYY-MM-DD HH:MM:SS)
        if since.tzinfo is not None:
            since_naive = since.replace(tzinfo=None)
        else:
            since_naive = since
        since_str = since_naive.strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self.db.execute(
            """SELECT filter_results, failed_filters FROM filter_outcomes
            WHERE processed_at >= ?""",
            (since_str,),
        )
        rows = await cursor.fetchall()

        per_filter = {}  # name -> {"total": N, "failed": N}
        for row in rows:
            try:
                results = _json.loads(row["filter_results"]) if row["filter_results"] else {}
                failed = _json.loads(row["failed_filters"]) if row["failed_filters"] else []
            except Exception:
                continue

            for fname, fres in results.items():
                if fname not in per_filter:
                    per_filter[fname] = {"total": 0, "failed": 0}
                per_filter[fname]["total"] += 1
                if fname in failed:
                    per_filter[fname]["failed"] += 1

        out = {}
        for fname, counts in per_filter.items():
            total = counts["total"]
            failed = counts["failed"]
            out[fname] = {
                "total": total,
                "failed": failed,
                "pass_rate": ((total - failed) / total * 100) if total > 0 else 0.0,
            }
        return out

    # Phase 1: trading bot persistence (paper mode)

    async def save_position(self, position) -> int:
        """Save new position. Returns row id."""
        cursor = await self.db.execute(
            """INSERT INTO positions
            (token_address, token_symbol, side, entry_tx_sig, entry_price,
             entry_amount_sol, entry_amount_token, entry_time, peak_price,
             current_amount_token, total_sold_sol, status, filter_params_version, paper,
             raw_gmgn_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.token_address, position.token_symbol, position.side,
                position.entry_tx_sig, position.entry_price,
                position.entry_amount_sol, position.entry_amount_token,
                position.entry_time.isoformat() if position.entry_time else None,
                position.peak_price, position.current_amount_token,
                position.total_sold_sol,
                position.status, position.filter_params_version,
                1 if position.paper else 0,
                getattr(position, "raw_gmgn_json", "") or "",
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def update_position(self, position) -> None:
        """Update mutable position fields. Accepts dict or dataclass."""
        def _g(key, default=None):
            if isinstance(position, dict):
                return position.get(key, default)
            return getattr(position, key, default)
        exit_time = _g("exit_time")
        await self.db.execute(
            """UPDATE positions SET
            peak_price = ?, current_amount_token = ?, total_sold_sol = ?, status = ?,
            exit_tx_sig = ?, exit_price = ?, exit_time = ?,
            pnl_sol = ?, pnl_pct = ?, hold_seconds = ?, exit_reason = ?
            WHERE id = ?""",
            (
                _g("peak_price", 0), _g("current_amount_token", 0),
                _g("total_sold_sol", 0), _g("status", "OPEN"),
                _g("exit_tx_sig", ""), _g("exit_price", 0),
                exit_time.isoformat() if exit_time else None,
                _g("pnl_sol", 0), _g("pnl_pct", 0), _g("hold_seconds", 0),
                _g("exit_reason", ""), _g("id"),
            ),
        )
        await self.db.commit()

    async def get_open_positions(self):
        """Return all OPEN positions as list of dicts (caller maps to dataclass)."""
        cursor = await self.db.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY entry_time DESC"
        )
        rows = await cursor.fetchall()
        return [self._row_to_position(r) for r in rows]

    async def save_trade(self, trade) -> int:
        """Save a Trade (BUY or SELL). Returns row id."""
        cursor = await self.db.execute(
            """INSERT INTO trades
            (position_id, side, tx_signature, amount_in, amount_out, price,
             fee_sol, slippage_bps, priority_fee_sol, jito_tip_sol, slot, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.position_id, trade.side, trade.tx_signature,
                trade.amount_in, trade.amount_out, trade.price,
                trade.fee_sol, trade.slippage_bps, trade.priority_fee_sol,
                trade.jito_tip_sol, trade.slot, trade.status, trade.error,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def save_risk_event(self, event_type: str, token_address: str = "",
                                details: str = "", paper: bool = True) -> int:
        """Log a risk event (daily_loss, position_limit, etc)."""
        cursor = await self.db.execute(
            """INSERT INTO risk_events
            (event_type, token_address, details, paper) VALUES (?, ?, ?, ?)""",
            (event_type, token_address, details, 1 if paper else 0),
        )
        await self.db.commit()
        return cursor.lastrowid

    def _row_to_position(self, row) -> dict:
        from datetime import datetime
        return {
            "id": row["id"],
            "token_address": row["token_address"],
            "token_symbol": row["token_symbol"],
            "side": row["side"],
            "entry_tx_sig": row["entry_tx_sig"] or "",
            "entry_price": row["entry_price"] or 0.0,
            "entry_amount_sol": row["entry_amount_sol"] or 0.0,
            "entry_amount_token": row["entry_amount_token"] or 0.0,
            "entry_time": datetime.fromisoformat(row["entry_time"]) if row["entry_time"] else None,
            "peak_price": row["peak_price"] or 0.0,
            "current_amount_token": row["current_amount_token"] or 0.0,
            "total_sold_sol": row["total_sold_sol"] if row["total_sold_sol"] is not None else 0.0,
            "status": row["status"],
            "exit_tx_sig": row["exit_tx_sig"] or "",
            "exit_price": row["exit_price"] or 0.0,
            "exit_time": datetime.fromisoformat(row["exit_time"]) if row["exit_time"] else None,
            "pnl_sol": row["pnl_sol"] or 0.0,
            "pnl_pct": row["pnl_pct"] or 0.0,
            "hold_seconds": row["hold_seconds"] or 0,
            "exit_reason": row["exit_reason"] or "",
            "filter_params_version": row["filter_params_version"] or 0,
            "raw_gmgn_json": row["raw_gmgn_json"] or "",
            "paper": bool(row["paper"]),
        }

    def _row_to_call(self, row) -> CallRecord:
        return CallRecord(
            id=row["id"],
            token_address=row["token_address"],
            token_name=row["token_name"] or "",
            token_symbol=row["token_symbol"] or "",
            call_time=datetime.fromisoformat(row["call_time"]) if row["call_time"] else None,
            entry_price=row["entry_price"] or 0.0,
            market_cap_at_call=row["market_cap_at_call"] or 0.0,
            volume_1h=row["volume_1h"] or 0.0,
            liquidity=row["liquidity"] or 0.0,
            holders_count=row["holders_count"] or 0,
            llm_score=row["llm_score"] or 0,
            llm_verdict=row["llm_verdict"] or "",
            llm_reasoning=row["llm_reasoning"] or "",
            llm_confidence=row["llm_confidence"] or 0.0,
            llm_key_factors=row["llm_key_factors"] or "",
            filter_params_version=row["filter_params_version"] or 1,
            feature_vector=row["feature_vector"] or "",
            status=CallStatus(row["status"]) if row["status"] else CallStatus.PENDING,
            max_gain=row["max_gain"] or 1.0,
            max_gain_time=datetime.fromisoformat(row["max_gain_time"]) if row["max_gain_time"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        )
