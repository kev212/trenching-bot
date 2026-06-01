"""Test Phase E2-Alert: 3 new tables + save/query methods."""
import sys
import asyncio
sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from storage.database import Database


async def run_tests():
    import os
    db_path = "/tmp/test_phase_e2.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    db = Database(db_path)
    await db.init()

    # Verify all 3 new tables exist
    cursor = await db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [r[0] for r in await cursor.fetchall()]
    assert "filter_outcomes" in tables, f"filter_outcomes missing. Tables: {tables}"
    assert "skip_decisions" in tables, f"skip_decisions missing. Tables: {tables}"
    assert "loss_analyses" in tables, f"loss_analyses missing. Tables: {tables}"
    print(f"✓ All 3 new tables exist: filter_outcomes, skip_decisions, loss_analyses")

    # Test save_filter_outcome (pass)
    await db.save_filter_outcome(
        token_address="ABC123",
        token_name="TestPass",
        token_symbol="TPASS",
        market_cap=50000,
        holders_count=300,
        age_minutes=15.0,
        filter_results={"min_market_cap": {"passed": True, "value": 50000}, "min_holders": {"passed": True, "value": 300}},
        passed=True,
        failed_filters=[],
        filter_params_version=5,
    )
    print("✓ save_filter_outcome (pass) OK")

    # Test save_filter_outcome (fail)
    await db.save_filter_outcome(
        token_address="XYZ789",
        token_name="TestFail",
        token_symbol="TFAIL",
        market_cap=2000,
        holders_count=50,
        age_minutes=5.0,
        filter_results={"min_market_cap": {"passed": False, "value": 2000}},
        passed=False,
        failed_filters=["min_market_cap", "min_holders"],
        was_retried=True,
        retry_count=2,
        filter_params_version=5,
    )
    print("✓ save_filter_outcome (fail) OK")

    # Test save_skip_decision
    await db.save_skip_decision(
        token_address="SKIP1",
        token_name="SkipToken",
        token_symbol="SKIP",
        llm_score=35,
        llm_reasoning="Low social score, no catalysts",
        llm_key_factors=["weak_social", "no_influencers"],
        market_cap=35000,
        holders_count=200,
        age_minutes=20.0,
        top15_pct=45.0,
        social_score=10.0,
        feature_vector={"min_market_cap": {"passed": True}},
    )
    print("✓ save_skip_decision OK")

    # Test save_loss_analysis
    await db.save_loss_analysis(
        call_id=42,
        token_address="LOSS1",
        token_symbol="LOSS",
        root_cause="Holder distribution too concentrated",
        wrong_filter="holder_distribution",
        suggestion="Lower top10 threshold from 50% to 40%",
        pattern="Dev wallet held 35%",
        confidence=0.85,
        llm_raw='{"root_cause": "..."}',
        max_gain=0.85,
        elapsed_seconds=1800.0,
    )
    print("✓ save_loss_analysis OK")

    # Test get_filter_performance_since
    from datetime import datetime, timezone, timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    perf = await db.get_filter_performance_since(since)
    assert "min_market_cap" in perf, f"min_market_cap missing from perf: {perf}"
    assert "min_holders" in perf, f"min_holders missing from perf: {perf}"
    assert perf["min_market_cap"]["failed"] == 1, f"Expected 1 fail for min_market_cap, got {perf['min_market_cap']}"
    assert perf["min_market_cap"]["total"] == 2, f"Expected 2 total for min_market_cap, got {perf['min_market_cap']}"
    print(f"✓ get_filter_performance_since OK: {perf}")

    # Verify row counts
    for table in ["filter_outcomes", "skip_decisions", "loss_analyses"]:
        cur = await db.db.execute(f"SELECT COUNT(*) as c FROM {table}")
        row = await cur.fetchone()
        assert row["c"] > 0, f"{table} should have rows, got {row['c']}"
        print(f"✓ {table}: {row['c']} rows")

    await db.close()
    print("\n✅ All Phase E2-Alert tests passed!")


if __name__ == "__main__":
    asyncio.run(run_tests())
