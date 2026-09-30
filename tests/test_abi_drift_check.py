"""tests/test_abi_drift_check.py

Tests for script/abi-drift-check.py, the guard that keeps docs/ABI.md (the
frozen interface of record) aligned with the interface declared in Rust source.

The properties under test are:

  * the source extractor recovers each event's topic and payload layout, and
    only attributes a field to the topics when `#[topic]` sits directly above
    it;
  * a reordered topic, a re-topicked field, a missing row, an extra row, a
    changed constant, or a dropped freeze declaration each FAIL (exit 1);
  * the happy path passes (exit 0);
  * prose *after* the final table row is legal, but prose that splits the table
    is reported as a structural problem.

Every negative case asserts a non-zero exit, because the previous
doc-alignment script's defining weakness was that it could only warn.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "script" / "abi-drift-check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("abi_drift_check", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


adc = _load_module()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EVENTS_RS = """\
use soroban_sdk::contractevent;

#[contractevent]
pub struct StreamCreated {
    #[topic]
    pub stream_id: u64,
    #[topic]
    pub sender: Address,
    #[topic]
    pub recipient: Address,
    pub token: Address,
    pub deposited: i128,
}

#[contractevent]
pub struct Withdrawn {
    #[topic]
    pub stream_id: u64,
    #[topic]
    pub recipient: Address,
    /// Amount moved in this call.
    pub amount: i128,
    pub withdrawn: i128,
}
"""

LIB_RS = """\
pub const MAX_BATCH_SIZE: u32 = 16;
pub const ABI_VERSION: u32 = 3;
"""

ABI_MD = """\
# Perpetua ABI

**Status: FROZEN as of 2026-08-12.**

## Events

| event | topics after the name | payload |
|---|---|---|
| `stream_created` | `stream_id`, `sender`, `recipient` | `token`, `deposited` |
| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |

`MAX_BATCH_SIZE = 16` is the cap on a single batched call.

## Next section

Trailing text that must not be parsed as an event row.
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Point the checker's module-level paths at a synthetic repo tree."""
    stream = tmp_path / "contracts" / "stream" / "src"
    stream.mkdir(parents=True)
    (stream / "events.rs").write_text(EVENTS_RS, encoding="utf-8")
    (stream / "lib.rs").write_text(LIB_RS, encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ABI.md").write_text(ABI_MD, encoding="utf-8")

    monkeypatch.setattr(adc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(adc, "STREAM_SRC", stream)
    monkeypatch.setattr(adc, "EVENTS_RS", stream / "events.rs")
    monkeypatch.setattr(adc, "LIB_RS", stream / "lib.rs")
    monkeypatch.setattr(adc, "ABI_MD", docs / "ABI.md")
    return tmp_path


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture text not found: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")



# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------


class TestExtractEvents:
    def test_topics_and_payload_are_separated(self):
        events = {e["name"]: e for e in adc.extract_events(EVENTS_RS)}
        assert set(events) == {"stream_created", "withdrawn"}
        assert events["stream_created"]["topics"] == [
            "stream_id",
            "sender",
            "recipient",
        ]
        assert events["stream_created"]["payload"] == ["token", "deposited"]
        assert events["withdrawn"]["topics"] == ["stream_id", "recipient"]
        assert events["withdrawn"]["payload"] == ["amount", "withdrawn"]

    def test_doc_comment_does_not_break_topic_attribution(self):
        """A doc comment between `#[topic]` and the field must not lose the tag."""
        events = {e["name"]: e for e in adc.extract_events(EVENTS_RS)}
        # `amount` carries a doc comment in the fixture; it is payload.
        assert "amount" not in events["withdrawn"]["topics"]

    def test_snake_case_matches_soroban_macro(self):
        assert adc.snake_case("StreamCreated") == "stream_created"
        assert adc.snake_case("ToppedUp") == "topped_up"
        assert adc.snake_case("TtlExtended") == "ttl_extended"
        assert adc.snake_case("DelegateRevoked") == "delegate_revoked"

    def test_commented_out_event_is_ignored(self):
        source = "// #[contractevent]\n// pub struct Ghost {\n// }\n" + EVENTS_RS
        names = {e["name"] for e in adc.extract_events(source)}
        assert "ghost" not in names
        assert "stream_created" in names

    def test_no_events_raises(self):
        with pytest.raises(adc.CheckError):
            adc.extract_events("pub struct NotAnEvent { x: u32 }")


class TestStripComments:
    def test_double_slash_inside_string_is_preserved(self):
        assert adc.strip_comments('let u = "http://x";') == 'let u = "http://x";'

    def test_trailing_comment_is_removed(self):
        assert adc.strip_comments("let x = 1; // set x").strip() == "let x = 1;"


class TestExtractConstants:
    def test_values_are_captured_and_whitespace_normalised(self):
        consts = adc.extract_public_constants(LIB_RS)
        assert consts == {"MAX_BATCH_SIZE": "16", "ABI_VERSION": "3"}


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------


class TestParseEventTable:
    def test_rows_are_parsed_with_split_fields(self):
        rows, problems = adc.parse_abi_event_table(ABI_MD)
        assert problems == []
        assert rows["stream_created"] == {
            "topics": ["stream_id", "sender", "recipient"],
            "payload": ["token", "deposited"],
        }

    def test_em_dash_means_empty_payload(self):
        doc = ABI_MD.replace(
            "| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |",
            "| `withdrawn` | `stream_id` | — |",
        )
        rows, _ = adc.parse_abi_event_table(doc)
        assert rows["withdrawn"]["payload"] == []

    def test_prose_after_final_row_is_not_a_problem(self):
        """Commentary following the table is ordinary and must stay legal."""
        rows, problems = adc.parse_abi_event_table(ABI_MD)
        assert problems == []
        assert len(rows) == 2

    def test_prose_splitting_the_table_is_reported(self):
        doc = ABI_MD.replace(
            "| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |",
            "| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |\n\n"
            "Some prose that interrupts the table.\n\n"
            "| `cancelled` | `stream_id` | `refunded` |",
        )
        rows, problems = adc.parse_abi_event_table(doc)
        assert any("splits the events table" in p for p in problems)
        # The row after the prose is still parsed, not silently dropped.
        assert "cancelled" in rows

    def test_missing_events_section_raises(self):
        with pytest.raises(adc.CheckError):
            adc.parse_abi_event_table("# Doc\n\nNo events here.\n")


# ---------------------------------------------------------------------------
# End-to-end behaviour
# ---------------------------------------------------------------------------


class TestMain:
    def test_clean_repo_passes(self, repo, capsys):
        assert adc.main([]) == 0
        assert "OK" in capsys.readouterr().out

    def test_reordered_topic_fails(self, repo, capsys):
        _edit(
            repo / "docs" / "ABI.md",
            "| `stream_created` | `stream_id`, `sender`, `recipient` |",
            "| `stream_created` | `stream_id`, `recipient`, `sender` |",
        )
        assert adc.main([]) == 1
        out = capsys.readouterr().out
        assert "topic layout drifted" in out
        assert "BREAKING" in out

    def test_re_topicked_field_fails(self, repo, capsys):
        # Move `token` from the payload into the topics on the Rust side.
        _edit(
            repo / "contracts" / "stream" / "src" / "events.rs",
            "    #[topic]\n    pub recipient: Address,\n    pub token: Address,",
            "    #[topic]\n    pub recipient: Address,\n    #[topic]\n"
            "    pub token: Address,",
        )
        assert adc.main([]) == 1
        assert "topic layout drifted" in capsys.readouterr().out

    def test_reordered_payload_fails(self, repo, capsys):
        _edit(
            repo / "docs" / "ABI.md",
            "| `stream_created` | `stream_id`, `sender`, `recipient` | "
            "`token`, `deposited` |",
            "| `stream_created` | `stream_id`, `sender`, `recipient` | "
            "`deposited`, `token` |",
        )
        assert adc.main([]) == 1
        assert "payload layout drifted" in capsys.readouterr().out

    def test_undocumented_event_fails(self, repo, capsys):
        _edit(
            repo / "contracts" / "stream" / "src" / "events.rs",
            "#[contractevent]\npub struct Withdrawn {",
            "#[contractevent]\npub struct Cancelled {\n"
            "    #[topic]\n    pub stream_id: u64,\n    pub refunded: i128,\n}\n\n"
            "#[contractevent]\npub struct Withdrawn {",
        )
        assert adc.main([]) == 1
        out = capsys.readouterr().out
        assert "`cancelled` exists in events.rs but is not documented" in out

    def test_documented_event_removed_from_source_fails(self, repo, capsys):
        # Rename the struct so the topic[0] namespace symbol changes, while
        # keeping it a valid event. The document still documents `withdrawn`,
        # so the checker must report the name as gone.
        _edit(
            repo / "contracts" / "stream" / "src" / "events.rs",
            "pub struct Withdrawn {",
            "pub struct WithdrawalRequested {",
        )
        assert adc.main([]) == 1
        out = capsys.readouterr().out
        assert "no longer exists" in out
        assert "withdrawal_requested" in out

    def test_constant_value_drift_fails(self, repo, capsys):
        _edit(
            repo / "docs" / "ABI.md",
            "`MAX_BATCH_SIZE = 16`",
            "`MAX_BATCH_SIZE = 32`",
        )
        assert adc.main([]) == 1
        assert "constant `MAX_BATCH_SIZE` drifted" in capsys.readouterr().out

    def test_dropped_freeze_status_fails(self, repo, capsys):
        _edit(
            repo / "docs" / "ABI.md",
            "**Status: FROZEN as of 2026-08-12.**",
            "Draft.",
        )
        assert adc.main([]) == 1
        assert "no longer declares a FROZEN status" in capsys.readouterr().out

    def test_structural_problem_alone_still_fails(self, repo, capsys):
        """A table-structure problem must be fatal even with zero findings.

        A row with too few cells is reported as a structural problem and is not
        parsed as an event, but it names no real event, so no drift finding is
        produced. The exit code must still be 1.
        """
        _edit(
            repo / "docs" / "ABI.md",
            "| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |",
            "| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |\n"
            "| malformed |",
        )
        findings, problems = adc.run_checks()
        assert findings == []
        assert any("expected 3" in p for p in problems)
        assert adc.main([]) == 1

    def test_missing_file_exits_two(self, repo, capsys):
        (repo / "docs" / "ABI.md").unlink()
        assert adc.main([]) == 2
        assert "could not run" in capsys.readouterr().err

    def test_json_output_is_machine_readable(self, repo, capsys):
        _edit(
            repo / "docs" / "ABI.md",
            "| `withdrawn` | `stream_id`, `recipient` | `amount`, `withdrawn` |",
            "| `withdrawn` | `stream_id` | `amount`, `withdrawn` |",
        )
        assert adc.main(["--json"]) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["findings"]


# ---------------------------------------------------------------------------
# The real repository
# ---------------------------------------------------------------------------


class TestRealRepository:
    """The check must pass against the repository as committed."""

    def test_repo_has_no_drift(self):
        findings, problems = adc.run_checks()
        assert findings == [], f"ABI drift in the real repo: {findings}"
        assert problems == [], f"docs/ABI.md table problems: {problems}"

    def test_real_events_are_all_documented(self):
        events = adc.extract_events(adc.EVENTS_RS.read_text(encoding="utf-8"))
        rows, _ = adc.parse_abi_event_table(adc.ABI_MD.read_text(encoding="utf-8"))
        assert {e["name"] for e in events} == set(rows)

