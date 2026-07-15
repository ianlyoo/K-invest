#!/usr/bin/env python3
"""margin-ta CLI subprocess runner.

Wraps the margin-ta skill's CLI in a convenient Python interface.
Uses margin-ta's own venv to avoid dependency conflicts.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from kinvest_common import NotConfiguredError, cache_dir

logger = logging.getLogger("k-invest.margin_ta")


def _extract_plan(plan: dict[str, Any], compact: bool = False) -> dict[str, Any]:
    entry_obj: dict[str, Any] = {
        "trigger": plan.get("trigger", ""),
        "type": plan.get("type", ""),
        "entry": plan.get("entry"),
        "stop_loss": plan.get("stop"),
        "targets": plan.get("targets", []),
        "quality": plan.get("quality"),
        "confidence": plan.get("confidence"),
        "first_target_rr": plan.get("first_target_rr"),
        "entry_tranche_pct": plan.get("entry_tranche_pct"),
        "position_sizing_note": plan.get("position_sizing_note"),
    }
    if not compact:
        entry_obj.update({
            "risk_pct": plan.get("risk_pct"),
            "hold_period": plan.get("hold_period"),
            "invalidation": plan.get("invalidation"),
            "next_conditions": plan.get("next_conditions", []),
            "rr_warning": plan.get("rr_warning"),
        })
    return entry_obj


def _subprocess_env(toss_cache: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("TOSS_TOKEN_CACHE", str(toss_cache))
    return env


class MarginTARunner:
    """Run margin-ta analysis via subprocess."""

    def __init__(self):
        raw_home = os.environ.get("MARGIN_TA_HOME", "").strip()
        if not raw_home:
            raise NotConfiguredError(
                "margin_ta",
                "margin-ta is not configured — set MARGIN_TA_HOME to a margin-ta "
                "checkout to enable analyze_technical/get_entry_plan/scan_top_stocks",
            )
        home = Path(raw_home).expanduser()
        self._python = home / ".venv" / "bin" / "python3"
        self._analyze_script = home / "scripts" / "margin_ta.py"
        self._scan_script = home / "scripts" / "scan_nightly.py"
        self._toss_cache = cache_dir() / "margin_ta_toss_token.json"
        if not self._python.exists():
            raise RuntimeError(
                f"margin-ta venv python not found at {self._python}. "
                "Create the venv inside MARGIN_TA_HOME first."
            )

    def analyze(
        self,
        symbol: str,
        market: str = "auto",
        no_market: bool = True,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run full technical analysis for a symbol.

        Args:
            symbol: Stock ticker (e.g., 'AAPL', '005930')
            market: 'auto', 'us', or 'kr'
            no_market: Skip VIX/breadth market regime (faster)
            extra_args: Additional CLI args

        Returns:
            Parsed JSON from margin-ta --json output.
        """
        args = [
            str(self._python),
            str(self._analyze_script),
            symbol,
            "--json",
            "--quiet",
            "--no-tv",
            "--no-session-quote",
            "--no-options",
        ]
        if market != "auto":
            args.extend(["--market", market])
        if no_market:
            args.append("--no-market")
        if extra_args:
            args.extend(extra_args)

        env = _subprocess_env(self._toss_cache)
        logger.info("Running margin-ta: %s", " ".join(args))
        result = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
        if result.returncode != 0:
            return {
                "error": True,
                "message": f"margin-ta failed with return code {result.returncode}",
                "stderr": result.stderr[:500],
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "error": True,
                "message": "Failed to parse margin-ta JSON output",
                "stdout": result.stdout[:500],
            }
        if _has_toss_401_warning(payload):
            try:
                self._toss_cache.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning("Retrying margin-ta once after Toss candles 401")
            retry = subprocess.run(args, capture_output=True, text=True, timeout=120, env=env)
            if retry.returncode == 0:
                try:
                    return json.loads(retry.stdout)
                except json.JSONDecodeError:
                    pass
        return payload

    def get_entry_plan(self, symbol: str, market: str = "auto", detail_level: str = "summary") -> dict[str, Any]:
        """Run analysis and return only the entry plan.

        Args:
            symbol: Stock ticker
            market: 'auto', 'us', or 'kr'

        Returns:
            Dict with recommended entry strategy, stop loss, target prices.
        """
        full = self.analyze(symbol, market=market)
        if isinstance(full, dict) and "error" in full:
            return full

        pricing = full.get("pricing", {})
        entry_plans = pricing.get("entry_plans", {})

        # entry_plans is a dict: {recommended, alternatives, all_plans, summary}
        signals = full.get("signals", {})
        entry_score_data = signals.get("entry_score", {})
        if isinstance(entry_score_data, dict):
            score_val = entry_score_data.get("score")
            score_verdict = entry_score_data.get("verdict", "")
        else:
            score_val = entry_score_data
            score_verdict = ""

        result: dict[str, Any] = {
            "symbol": symbol.upper(),
            "current_price": full.get("current_price"),
            "entry_score": score_val,
            "entry_rating": score_verdict,
            "recommended_plan": None,
            "alternatives": [],
            "summary": "",
        }

        if isinstance(entry_plans, dict):
            result["summary"] = entry_plans.get("summary", "")
            recommended = entry_plans.get("recommended")
            if recommended:
                result["recommended_plan"] = _extract_plan(recommended)
            if detail_level != "summary":
                for plan in entry_plans.get("alternatives", []):
                    result["alternatives"].append(_extract_plan(plan, compact=True))
            if detail_level == "full":
                result["raw_analysis"] = full

        return result

    def scan_top_stocks(self, top_n: int = 5, min_score: int = 0) -> dict[str, Any]:
        """Scan top-N stocks from NASDAQ 100 + S&P 500.

        Uses cached OHLCV if available (fast), otherwise downloads (slow).
        Estimated time: ~3min with cache, ~20min without.

        Args:
            top_n: Number of top stocks to return (default 5)
            min_score: Minimum entry score filter (default 0)

        Returns:
            Dict with top stocks ranked by entry score.
        """
        args = [
            str(self._python),
            str(self._scan_script),
            "--json",
            "--top", str(top_n),
        ]
        if min_score > 0:
            args.extend(["--min-score", str(min_score)])

        logger.info("Running margin-ta scanner: %s", " ".join(args))
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=1800,  # 30 min max
            env=_subprocess_env(self._toss_cache),
        )
        if result.returncode != 0:
            return {
                "error": True,
                "message": f"Scan failed with return code {result.returncode}",
                "stderr": result.stderr[:500],
            }
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {
                "error": True,
                "message": "Failed to parse scan JSON output",
                "stdout": result.stdout[:500],
            }


def _has_toss_401_warning(payload: dict[str, Any]) -> bool:
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        return False
    return any("토스 candles" in str(item) and "401" in str(item) for item in warnings)
