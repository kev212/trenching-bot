import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from analysis.models import CallRecord, PriceSnapshot, CallStatus

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
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()

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
        now = datetime.utcnow().isoformat()
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
                snapshot.snapshot_time.isoformat() if snapshot.snapshot_time else datetime.utcnow().isoformat(),
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
        new_value: float, reason: str, confidence: float, win_rate_before: float
    ):
        await self.db.execute(
            """INSERT INTO filter_adjustments
            (filter_name, param_name, old_value, new_value, reason, confidence, applied_at, win_rate_before)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (filter_name, param_name, old_value, new_value, reason, confidence,
             datetime.utcnow().isoformat(), win_rate_before),
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
            (datetime.utcnow().isoformat(), reason, adjustment_id),
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
