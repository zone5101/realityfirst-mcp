from realityfirst_mcp.claims import classify_claim
from realityfirst_mcp.compaction import compact_evidence, record_loss
from realityfirst_mcp.evidence import (
    check_completion_evidence,
    resolve_precedence,
    validate_evidence,
)
from realityfirst_mcp.policy import replay_gate


def test_placeholder_file_write_claim_fails_without_readback():
    classified = classify_claim("Files written successfully")
    assert classified["claim_type"] == "file_written"

    result = check_completion_evidence(
        "file_written",
        [{"type": "model_report", "valid": True, "ref": "agent-output"}],
    )
    assert result["verdict"] == "FAIL"


def test_file_write_readback_alone_is_not_enough():
    result = check_completion_evidence(
        "file_written",
        [{"type": "readback", "valid": True, "ref": "file://summary.json"}],
    )
    assert result["verdict"] == "FAIL"
    assert ["artifact_stat", "hash"] in result["missing_requirement_groups"]


def test_fake_hash_label_cannot_satisfy_hash_gate():
    result = check_completion_evidence(
        "file_written",
        [
            {"type": "readback", "valid": True, "ref": "file://summary.json"},
            {
                "type": "hash",
                "valid": True,
                "ref": "file://summary.json",
                "algorithm": "json_parse",
                "digest": "success",
            },
        ],
    )
    assert result["verdict"] == "FAIL"
    assert result["rejected_evidence"][0]["type"] == "hash"
    assert ["artifact_stat", "hash"] in result["missing_requirement_groups"]


def test_file_write_passes_with_real_sha256():
    result = check_completion_evidence(
        "file_written",
        [
            {"type": "readback", "valid": True, "ref": "file://summary.json"},
            {
                "type": "hash",
                "valid": True,
                "ref": "file://summary.json",
                "algorithm": "sha256",
                "digest": "a" * 64,
            },
        ],
    )
    assert result["verdict"] == "PASS"


def test_file_write_passes_with_structured_artifact_stat():
    result = check_completion_evidence(
        "file_written",
        [
            {"type": "readback", "valid": True, "ref": "file://summary.json"},
            {
                "type": "artifact_stat",
                "valid": True,
                "path": "C:/tmp/summary.json",
                "size_bytes": 535,
                "mtime": "2026-10-06T20:58:19Z",
            },
        ],
    )
    assert result["verdict"] == "PASS"


def test_artifact_stat_without_mtime_is_rejected():
    checked = validate_evidence([
        {
            "type": "artifact_stat",
            "path": "C:/tmp/summary.json",
            "size_bytes": 535,
        }
    ])
    assert checked["invalid_count"] == 1
    assert "artifact_stat requires mtime/modified_at" in checked["items"][0]["reasons"]


def test_tests_pass_with_terminal_receipt():
    result = check_completion_evidence(
        "tests_passed",
        [{"type": "terminal_receipt", "valid": True, "ref": "receipt://123"}],
    )
    assert result["verdict"] == "PASS"


def test_live_reality_beats_history():
    result = resolve_precedence([
        {"source_class": "history", "value": "old", "ref": "history.md"},
        {"source_class": "live_reality", "value": "new", "ref": "runtime://now"},
    ])
    assert result["winner"]["value"] == "new"
    assert len(result["conflicts"]) == 1


def test_unknown_do_not_replay_is_blocked():
    result = replay_gate("UNKNOWN_DO_NOT_REPLAY")
    assert result.verdict == "BLOCK"


def test_unknown_side_effect_is_blocked():
    result = replay_gate("FAILED", "UNKNOWN")
    assert result.verdict == "BLOCK"


def test_compaction_prioritizes_reality_and_dedupes():
    result = compact_evidence([
        {"type": "model_report", "ref": "same"},
        {"type": "runtime_probe", "ref": "same"},
        {"type": "history", "ref": "history"},
    ], max_items=2)
    assert result["selected"][0]["type"] == "runtime_probe"
    assert len(result["selected"]) == 2


def test_loss_entry_shape():
    entry = record_loss(
        "files written",
        "disk still contained placeholders",
        "files were not materialized",
        ["file://a"],
        "read back claimed outputs before completion",
    )
    assert entry["prevention_rule"].startswith("read back")
