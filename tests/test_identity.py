"""Pre-release identity migration must not rewrite retained historical evidence."""
import hashlib
import json
from pathlib import Path
import tomllib

from retryloom.replay.evaluator import evaluate
from retryloom.replay.models import ReplayReport


ROOT = Path(__file__).parents[1]
ARCHIVE = ROOT / "benchmarks/results/csv-fixture-neutral-20261008"


def test_canonical_package_identity():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["project"]["name"] == "retryloom"
    assert config["project"]["scripts"] == {"retryloom": "retryloom.main:app"}
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/retryloom"]
    assert not (ROOT / "src/reflex").exists()


def test_retained_historical_artifact_hashes():
    if not ARCHIVE.exists():
        # Historical binaries are deliberately excluded from the source distribution.
        import pytest
        pytest.skip("retained audit archive is not distributed in the package")
    hashes = json.loads((ARCHIVE / "artifact-sha256.json").read_text())
    for name, expected in hashes.items():
        assert hashlib.sha256((ARCHIVE / name).read_bytes()).hexdigest() == expected


def test_historical_report_still_evaluates_without_rewriting_evidence():
    if not ARCHIVE.exists():
        import pytest
        pytest.skip("retained audit archive is not distributed in the package")
    raw = (ARCHIVE / "replay.json").read_bytes()
    report = ReplayReport.model_validate_json(raw)
    before = report.model_dump_json()
    result = evaluate(report)
    assert result.model_dump(mode="json") == json.loads((ARCHIVE / "evaluation.json").read_text())
    assert result.status == "rejected"
    assert result.control_success_count == result.reflex_success_count == 0
    assert report.model_dump_json() == before
    assert (ARCHIVE / "replay.json").read_bytes() == raw
