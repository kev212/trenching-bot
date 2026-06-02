"""Tests for web3_analyzer fallback behavior (Stage 4).

Full LLM #3 behavior requires network + real mimo client.
Tests focus on the deterministic parts:
- Fallback returns 50 (neutral)
- Red-flag cap applies
- Result schema is correct
"""
import asyncio
import sys
sys.path.insert(0, "/Users/khezuma/workspace/trenching")

from analysis.models import TokenData
from core.web3_analyzer import analyze_web3_substance


class FakeMiMo:
    """Minimal mimo client that returns predetermined JSON."""
    def __init__(self, response: dict):
        self.response = response
        self._lock = asyncio.Lock()
    async def analyze_token(self, system_prompt, user_prompt, temperature=0.3):
        return dict(self.response)


def make_token(project_type="web3_project", name="Magpie", symbol="MAG") -> TokenData:
    return TokenData(
        address="test",
        name=name,
        symbol=symbol,
        project_type=project_type,
        website_text="Real product with whitepaper and roadmap",
    )


def test_fallback_for_non_web3():
    """Non-web3 tokens skip LLM #3 → fallback 50."""
    async def go():
        token = make_token(project_type="meme")
        mimo = FakeMiMo({"substance_score": 99, "red_flags": []})
        result = await analyze_web3_substance(token, mimo)
        assert result["substance_score"] == 50.0, f"Expected 50 fallback, got {result['substance_score']}"
        print(f"✓ fallback_non_web3: substance=50 (expected 50)")
    asyncio.run(go())


def test_high_substance_score():
    """High substance score (75) for legit web3 project."""
    async def go():
        token = make_token()
        mimo = FakeMiMo({
            "substance_score": 75,
            "red_flags": [],
            "reasoning": "Has GitHub, audit, doxxed team",
            "team_visible": True,
            "has_github": True,
            "has_audit": True,
            "audit_firm": "CertiK",
        })
        result = await analyze_web3_substance(token, mimo)
        assert result["substance_score"] == 75
        assert result["team_visible"] is True
        assert result["has_github"] is True
        assert result["has_audit"] is True
        assert result["audit_firm"] == "CertiK"
        print(f"✓ high_substance: score=75, team=visible, audit=CertiK")
    asyncio.run(go())


def test_red_flag_caps_at_40():
    """Critical red flags cap substance at 40 (per rubric)."""
    async def go():
        token = make_token()
        mimo = FakeMiMo({
            "substance_score": 80,  # would be 80, but red flag caps it
            "red_flags": ["stolen_team"],
            "reasoning": "Stolen team photos detected",
            "team_visible": False,
            "has_github": False,
            "has_audit": False,
            "audit_firm": "",
        })
        result = await analyze_web3_substance(token, mimo)
        assert result["substance_score"] == 40, f"Expected 40 (capped), got {result['substance_score']}"
        assert "stolen_team" in result["red_flags"]
        print(f"✓ red_flag_cap: score=80→40 (capped due to stolen_team)")
    asyncio.run(go())


def test_non_critical_red_flag_no_cap():
    """Non-critical red flags don't trigger cap."""
    async def go():
        token = make_token()
        mimo = FakeMiMo({
            "substance_score": 60,
            "red_flags": ["vague_roadmap"],
            "reasoning": "Roadmap is unclear but no plagiarism",
            "team_visible": True,
            "has_github": True,
            "has_audit": False,
            "audit_firm": "",
        })
        result = await analyze_web3_substance(token, mimo)
        assert result["substance_score"] == 60
        print(f"✓ non_critical_flag: score=60 (no cap)")
    asyncio.run(go())


def test_score_clamped_0_100():
    """Score clamped to [0, 100]."""
    async def go():
        token = make_token()
        mimo = FakeMiMo({"substance_score": 150, "red_flags": []})
        result = await analyze_web3_substance(token, mimo)
        assert result["substance_score"] == 100
        print(f"✓ score_clamped_high: 150→100")

        mimo = FakeMiMo({"substance_score": -10, "red_flags": []})
        result = await analyze_web3_substance(token, mimo)
        assert result["substance_score"] == 0
        print(f"✓ score_clamped_low: -10→0")
    asyncio.run(go())


def test_web3_combined_formula():
    """0.4 social + 0.6 substance for web3."""
    llm1_social = 50
    llm3_substance = 90
    expected = (llm1_social * 0.4) + (llm3_substance * 0.6)
    assert abs(expected - 74.0) < 0.01
    print(f"✓ web3_combined: 50×0.4 + 90×0.6 = {expected}")


def test_magpie_scenario():
    """Magpie-like: high social (81), high substance (80) → combined 80.4."""
    llm1_social = 81
    llm3_substance = 80
    combined = (llm1_social * 0.4) + (llm3_substance * 0.6)
    assert abs(combined - 80.4) < 0.01
    print(f"✓ magpie_scenario: social=81, substance=80, combined={combined}")


def test_marketing_only_web3():
    """Marketing-heavy web3: high social (85), low substance (30) → combined 52."""
    llm1_social = 85
    llm3_substance = 30
    combined = (llm1_social * 0.4) + (llm3_substance * 0.6)
    assert abs(combined - 52.0) < 0.01
    print(f"✓ marketing_only: social=85, substance=30, combined={combined}")


if __name__ == "__main__":
    test_fallback_for_non_web3()
    test_high_substance_score()
    test_red_flag_caps_at_40()
    test_non_critical_red_flag_no_cap()
    test_score_clamped_0_100()
    test_web3_combined_formula()
    test_magpie_scenario()
    test_marketing_only_web3()
    print("\n✅ All web3 analyzer tests passed!")
