import json

import pytest

from scripts.gen_pricing import ALLOWLISTED_REMOVALS, _load_vendor

# The litellm → catalog transform (filter_bedrock, _build_entries, rate conversion)
# lives in lambda/proxy/pricing_catalog.py and is covered by test_pricing_catalog.py.
# This module covers the offline driver only: vendor loading + the coverage gate.


def _vendor_entry(inp=3e-6, out=15e-6, **extra):
    return {
        "litellm_provider": "bedrock",
        "input_cost_per_token": inp,
        "output_cost_per_token": out,
        **extra,
    }


def test_load_vendor_excludes_meta_key(tmp_path):
    path = tmp_path / "vendor.json"
    path.write_text(
        json.dumps(
            {
                "_meta": {"source": "x"},
                "amazon.nova-lite-v1:0": _vendor_entry(6e-8, 2.4e-7),
            }
        )
    )
    vendor = _load_vendor(path)
    assert "_meta" not in vendor
    assert "amazon.nova-lite-v1:0" in vendor


def test_coverage_regression_gate_exits_nonzero(tmp_path, monkeypatch):
    import scripts.gen_pricing as gp

    vendor_path = tmp_path / "vendor.json"
    snapshot_path = tmp_path / "snapshot.txt"
    vendor_path.write_text(
        json.dumps(
            {
                "_meta": {"source": "x"},
                "amazon.nova-lite-v1:0": _vendor_entry(6e-8, 2.4e-7),
            }
        )
    )
    # Snapshot references an ID with no vendor entry and not derivable.
    snapshot_path.write_text("amazon.nova-lite-v1:0\nmissing.model-v1:0\n")

    monkeypatch.setattr(gp, "VENDOR_JSON_PATH", vendor_path)
    monkeypatch.setattr(gp, "SNAPSHOT_PATH", snapshot_path)

    with pytest.raises(SystemExit) as exc:
        gp.main()
    assert exc.value.code == 1


def test_coverage_regression_gate_reads_temp_vendor_not_real_file(tmp_path, monkeypatch):
    # Genuine isolation: the gate must pass based on the TEMP vendor contents, not the
    # committed real vendor file. We use a synthetic ID that exists only in the temp
    # vendor; if main() ignored the monkeypatch and read the real file, this ID would
    # be missing from generated entries and the gate would (wrongly) fire SystemExit.
    import scripts.gen_pricing as gp

    vendor_path = tmp_path / "vendor.json"
    snapshot_path = tmp_path / "snapshot.txt"
    synthetic_id = "amazon.synthetic-only-in-temp-v1:0"
    vendor_path.write_text(
        json.dumps(
            {
                "_meta": {"source": "x"},
                synthetic_id: _vendor_entry(6e-8, 2.4e-7),
            }
        )
    )
    # Snapshot references only the synthetic ID + its derived us. variant.
    snapshot_path.write_text(f"{synthetic_id}\nus.{synthetic_id}\n")

    monkeypatch.setattr(gp, "VENDOR_JSON_PATH", vendor_path)
    monkeypatch.setattr(gp, "SNAPSHOT_PATH", snapshot_path)

    # No SystemExit: every snapshot ID is covered by the temp vendor (+ derivation).
    gp.main()


def test_coverage_regression_gate_allowlisted_no_exit(tmp_path, monkeypatch):
    import scripts.gen_pricing as gp

    vendor_path = tmp_path / "vendor.json"
    snapshot_path = tmp_path / "snapshot.txt"
    vendor_path.write_text(
        json.dumps(
            {
                "_meta": {"source": "x"},
                "amazon.nova-lite-v1:0": _vendor_entry(6e-8, 2.4e-7),
            }
        )
    )
    snapshot_path.write_text("amazon.nova-lite-v1:0\nmissing.model-v1:0\n")

    monkeypatch.setattr(gp, "VENDOR_JSON_PATH", vendor_path)
    monkeypatch.setattr(gp, "SNAPSHOT_PATH", snapshot_path)
    monkeypatch.setattr(gp, "ALLOWLISTED_REMOVALS", frozenset({"missing.model-v1:0"}))

    # Should print the catalog and return normally (no SystemExit).
    gp.main()


def test_allowlisted_removals_starts_empty():
    assert ALLOWLISTED_REMOVALS == frozenset()
