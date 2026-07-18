from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kinvest_common import NotConfiguredError
from margin_ta_runner import MarginTARunner


def test_runner_when_home_unset_then_not_configured(monkeypatch) -> None:
    monkeypatch.delenv("MARGIN_TA_HOME", raising=False)
    with pytest.raises(NotConfiguredError) as exc:
        MarginTARunner()
    assert exc.value.provider == "margin_ta"
    assert "MARGIN_TA_HOME" in str(exc.value)


def test_runner_when_venv_missing_then_runtime_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MARGIN_TA_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="venv python not found"):
        MarginTARunner()


def test_runner_when_venv_present_then_paths_derived(monkeypatch, tmp_path: Path) -> None:
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    monkeypatch.setenv("MARGIN_TA_HOME", str(tmp_path))

    runner = MarginTARunner()

    assert runner._python == venv_python
    assert runner._analyze_script == tmp_path / "scripts" / "margin_ta.py"
    assert runner._scan_script == tmp_path / "scripts" / "scan_nightly.py"


def test_summarize_analysis_extracts_new_blocks() -> None:
    from margin_ta_runner import _summarize_analysis

    full = {
        "horizons": {"short": {"stance": "bullish", "score": 40, "basis": ["x"]},
                     "mid": {"stance": "neutral", "score": 5, "basis": []},
                     "long": {"stance": "bullish", "score": 55, "basis": []},
                     "alignment": "aligned_bull"},
        "consensus": {"agreement": 72, "conflicts": ["momentum_vs_trend"],
                      "categories": {}, "divergence": None},
        "sr_tiers": {"key_below_top3": [{"price": 90.0}], "key_above_top3": [],
                     "major_supports": [], "major_resistances": []},
    }
    out = _summarize_analysis(full)
    assert out["horizons"]["short"] == {"stance": "bullish", "score": 40}
    assert out["alignment"] == "aligned_bull"
    assert out["consensus"] == {"agreement": 72, "conflicts": ["momentum_vs_trend"]}
    assert out["key_levels"]["below"] == [{"price": 90.0}]


def test_summarize_analysis_tolerates_old_payload() -> None:
    from margin_ta_runner import _summarize_analysis

    assert _summarize_analysis({"symbol": "AAPL"}) == {}
