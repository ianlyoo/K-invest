from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from kinvest_common import NotConfiguredError, apply_env_file, cache_dir, load_env_file


def test_cache_dir_when_env_unset_then_home_default(monkeypatch) -> None:
    monkeypatch.delenv("KINVEST_CACHE_DIR", raising=False)
    assert cache_dir() == Path.home() / ".cache" / "k-invest"


def test_cache_dir_when_env_set_then_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KINVEST_CACHE_DIR", str(tmp_path))
    assert cache_dir() == tmp_path


def test_load_env_file_parses_values_and_skips_comments(tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("# comment\nFOO=bar\nQUOTED='baz'\nBROKEN_LINE\n")
    assert load_env_file(env_file) == {"FOO": "bar", "QUOTED": "baz"}


def test_load_env_file_when_missing_then_empty(tmp_path: Path) -> None:
    assert load_env_file(tmp_path / "nope.env") == {}


def test_apply_env_file_does_not_override_existing(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("KINVEST_TEST_KEY=from_file\n")
    monkeypatch.setenv("KINVEST_TEST_KEY", "from_env")
    apply_env_file(env_file)
    assert os.environ["KINVEST_TEST_KEY"] == "from_env"


def test_apply_env_file_sets_missing_keys(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "test.env"
    env_file.write_text("KINVEST_TEST_KEY2=from_file\n")
    monkeypatch.delenv("KINVEST_TEST_KEY2", raising=False)
    apply_env_file(env_file)
    assert os.environ["KINVEST_TEST_KEY2"] == "from_file"


def test_not_configured_error_carries_provider() -> None:
    err = NotConfiguredError("kis", "KIS not configured")
    assert err.provider == "kis"
    assert isinstance(err, RuntimeError)
    assert "KIS not configured" in str(err)
