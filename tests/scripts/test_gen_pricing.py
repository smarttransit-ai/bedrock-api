import json

import pytest

from scripts.gen_pricing import (
    ALLOWLISTED_REMOVALS,
    _build_entries,
    _load_vendor,
    _per_token_to_micros_per_1k,
    _to_on_demand_rates,
)


def test_per_token_to_micros_per_1k_integer_rounding():
    assert _per_token_to_micros_per_1k(3e-6) == 3_000
    assert _per_token_to_micros_per_1k(15e-6) == 15_000
    assert _per_token_to_micros_per_1k(0.0) == 0
    assert _per_token_to_micros_per_1k(1e-6) == 1_000


def test_per_token_to_micros_per_1k_half_even_ties():
    # Real litellm Bedrock values that produce half-integer results.
    # Python round() uses banker's rounding (half-even); this is intentional —
    # sub-µUSD/1k ties are economically negligible.
    assert _per_token_to_micros_per_1k(3.125e-7) == 312  # 312.5 → 312 (half-even; 312 is even)
    assert _per_token_to_micros_per_1k(2.1875e-6) == 2188  # 2187.5 → 2188 (half-even; 2188 even)
    assert _per_token_to_micros_per_1k(8.25e-8) == 82  # 82.5 → 82 (half-even; 82 is even)


def test_to_on_demand_rates_full_entry():
    rates = _to_on_demand_rates(
        {
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 15e-6,
            "cache_read_input_token_cost": 3e-7,
            "cache_creation_input_token_cost": 3.75e-6,
        }
    )
    assert rates["input_usd_micros_per_1k"] == 3_000
    assert rates["output_usd_micros_per_1k"] == 15_000
    assert rates["cache_read_input_usd_micros_per_1k"] == 300
    assert rates["cache_write_input_usd_micros_per_1k"] == 3_750


def test_to_on_demand_rates_cache_absent_defaults_to_output_rate():
    rates = _to_on_demand_rates(
        {
            "input_cost_per_token": 6e-8,
            "output_cost_per_token": 2.4e-7,
        }
    )
    assert rates["output_usd_micros_per_1k"] == 240
    assert rates["cache_read_input_usd_micros_per_1k"] == 240
    assert rates["cache_write_input_usd_micros_per_1k"] == 240


def test_to_on_demand_rates_cache_null_defaults_to_output_rate():
    # Simulates JSON null loaded by json.load (-> Python None). Confirms null and
    # absent are handled identically (see D3).
    rates = _to_on_demand_rates(
        {
            "input_cost_per_token": 6e-8,
            "output_cost_per_token": 2.4e-7,
            "cache_read_input_token_cost": None,
            "cache_creation_input_token_cost": None,
        }
    )
    assert rates["cache_read_input_usd_micros_per_1k"] == 240
    assert rates["cache_write_input_usd_micros_per_1k"] == 240


def _vendor_entry(inp=3e-6, out=15e-6, **extra):
    return {
        "litellm_provider": "bedrock",
        "input_cost_per_token": inp,
        "output_cost_per_token": out,
        **extra,
    }


def test_build_entries_batch_mirrors_on_demand():
    entries, _ = _build_entries({"amazon.nova-lite-v1:0": _vendor_entry(6e-8, 2.4e-7)})
    e = entries["amazon.nova-lite-v1:0"]
    assert e["batch"] == e["on_demand"]


def test_build_entries_derives_cross_region_when_absent():
    entries, derived = _build_entries({"anthropic.claude-x-v1:0": _vendor_entry()})
    assert "us.anthropic.claude-x-v1:0" in entries
    assert "global.anthropic.claude-x-v1:0" in entries
    assert entries["us.anthropic.claude-x-v1:0"] == entries["anthropic.claude-x-v1:0"]
    assert entries["global.anthropic.claude-x-v1:0"] == entries["anthropic.claude-x-v1:0"]
    assert "us.anthropic.claude-x-v1:0" in derived
    assert "global.anthropic.claude-x-v1:0" in derived


def test_build_entries_derives_base_from_regional_variant():
    src = "us.amazon.nova-premier-v1:0"
    entries, derived = _build_entries({src: _vendor_entry(2.5e-6, 1.25e-5)})
    assert "amazon.nova-premier-v1:0" in entries
    assert entries["amazon.nova-premier-v1:0"] == entries[src]
    assert "amazon.nova-premier-v1:0" in derived
    # Source entry rates must be intact after derivation (explicit, non-vacuous).
    assert entries[src]["on_demand"]["input_usd_micros_per_1k"] == 2_500
    assert entries[src]["on_demand"]["output_usd_micros_per_1k"] == 12_500


def test_build_entries_does_not_expand_us_gov_variant():
    # us-gov.* is region-scoped and non-derivable: never prepend us./global. to it,
    # and never strip it to a phantom base. Regression for global.us-gov.* garbage.
    src = "us-gov.anthropic.claude-x-v1:0"
    entries, derived = _build_entries({src: _vendor_entry()})
    assert src in entries
    assert "us.us-gov.anthropic.claude-x-v1:0" not in entries
    assert "global.us-gov.anthropic.claude-x-v1:0" not in entries
    # Not stripped to a phantom base either.
    assert "anthropic.claude-x-v1:0" not in entries
    assert derived == []


def test_build_entries_does_not_expand_non_cross_region_provider():
    # ai21 / cohere have no AWS cross-region inference profiles: a base ID for such
    # a provider must NOT spawn us./global. variants (would be phantom IDs).
    entries, derived = _build_entries(
        {
            "ai21.jamba-1-5-mini-v1:0": _vendor_entry(),
            "cohere.command-r-v1:0": _vendor_entry(),
        }
    )
    assert "us.ai21.jamba-1-5-mini-v1:0" not in entries
    assert "global.ai21.jamba-1-5-mini-v1:0" not in entries
    assert "us.cohere.command-r-v1:0" not in entries
    assert "global.cohere.command-r-v1:0" not in entries
    assert derived == []


def test_build_entries_drops_zero_priced_entries():
    # Both token costs 0 (e.g. rerank): no billing value, dropped to avoid underbilling.
    entries, _ = _build_entries({"amazon.rerank-v1:0": _vendor_entry(0.0, 0.0)})
    assert "amazon.rerank-v1:0" not in entries


def test_build_entries_keeps_token_priced_embedding():
    # Embedding model: input > 0, output 0. Billable via /invoke — must be kept.
    entries, _ = _build_entries({"amazon.titan-embed-text-v1": _vendor_entry(1e-7, 0.0)})
    e = entries["amazon.titan-embed-text-v1"]
    assert e["on_demand"]["input_usd_micros_per_1k"] == 100
    assert e["on_demand"]["output_usd_micros_per_1k"] == 0


def test_build_entries_does_not_overwrite_explicit_cross_region():
    entries, _ = _build_entries(
        {
            "anthropic.claude-x": _vendor_entry(3e-6, 15e-6),
            "us.anthropic.claude-x": _vendor_entry(9e-6, 45e-6),
        }
    )
    assert entries["us.anthropic.claude-x"]["on_demand"]["input_usd_micros_per_1k"] == 9_000
    assert entries["anthropic.claude-x"]["on_demand"]["input_usd_micros_per_1k"] == 3_000


def test_build_entries_shallow_copy_avoids_aliasing():
    # Shallow copy (dict(...)) makes the per-ID mode-map dicts distinct objects, so
    # rebinding a mode on one derived ID does not leak to the source (see D5).
    entries, _ = _build_entries({"anthropic.claude-x": _vendor_entry()})
    base = entries["anthropic.claude-x"]
    derived = entries["us.anthropic.claude-x"]
    assert base is not derived
    derived["batch"] = {"input_usd_micros_per_1k": 1}
    assert base["batch"]["input_usd_micros_per_1k"] == 3_000


def test_build_entries_skips_unknown_provider():
    entry = _vendor_entry()
    entry["litellm_provider"] = "vertex_ai"
    entries, _ = _build_entries({"vertex.model": entry})
    assert "vertex.model" not in entries


def test_build_entries_skips_missing_cost_fields():
    entry = {"litellm_provider": "bedrock", "output_cost_per_token": 15e-6}
    entries, _ = _build_entries({"bedrock.broken": entry})
    assert "bedrock.broken" not in entries


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
