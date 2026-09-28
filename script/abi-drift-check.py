#!/usr/bin/env python3
"""Check that docs/ABI.md still matches the interface declared in Rust source.

`docs/ABI.md` is the frozen interface of record. This script is the mechanical
guard that keeps the freeze honest: it re-derives the public surface from the
contract sources and compares it against what the document claims.

What is checked
---------------
1. **Event topic layout** - for every `#[contractevent]` struct in
   `contracts/stream/src/events.rs`, the ordered list of `#[topic]` fields, and
   the ordered list of payload fields, must match the `## Events` table in
   `docs/ABI.md`. Reordering or re-topicking a field is a *breaking* ABI change
   under the rules the document itself states, so it must fail loudly.
2. **Event inventory** - the set of event names in the source and in the
   document must be identical. A new event that nobody documented (or a
   documented event that no longer exists) is drift.
3. **Public constants** - every `pub const` in `contracts/stream/src/lib.rs`
   that the document quotes must have the same value in both places.
4. **Freeze status** - the document must still declare itself frozen, so that
   "we froze it" and "we still mean it" cannot drift apart silently.

Deliberate non-goals
--------------------
This is a *static* check. It does not compile the contract, so it cannot catch
a change that the `#[contractevent]` macro expands differently than the source
text suggests. It is a fast, dependency-free pre-flight that runs on every PR;
the wasm spec comparison remains the authoritative gate.

Unlike the older doc-alignment warnings, every failure here is fatal: the
script exits non-zero. A checker that cannot fail is not a check.

Usage
-----
    python3 script/abi-drift-check.py            # check, print findings
    python3 script/abi-drift-check.py --json     # machine-readable

Exit codes: 0 = no drift, 1 = drift detected, 2 = could not run the check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

STREAM_SRC = REPO_ROOT / "contracts" / "stream" / "src"
EVENTS_RS = STREAM_SRC / "events.rs"
LIB_RS = STREAM_SRC / "lib.rs"
ABI_MD = REPO_ROOT / "docs" / "ABI.md"


class CheckError(RuntimeError):
    """Raised when the check cannot run at all (missing file, unparsable)."""


# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------


def strip_comments(source: str) -> str:
    """Remove `//` line comments so commented-out code is not parsed as real.

    Block comments are left intact: no comment in these files contains an
    unbalanced `*/`, and leaving them alone keeps this helper easy to audit.
    The quote tracking means a `//` inside a string literal is not a comment.
    """
    out = []
    for line in source.splitlines():
        idx = 0
        in_str = False
        quote = ""
        while idx < len(line):
            ch = line[idx]
            if in_str:
                if ch == "\\":
                    idx += 2
                    continue
                if ch == quote:
                    in_str = False
            elif ch in ('"', "'"):
                in_str = True
                quote = ch
            elif ch == "/" and idx + 1 < len(line) and line[idx + 1] == "/":
                break
            idx += 1
        out.append(line[:idx])
    return "\n".join(out)


def snake_case(name: str) -> str:
    """`StreamCreated` -> `stream_created`, matching the soroban macro."""
    step = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step).lower()


def extract_events(source: str) -> list[dict]:
    """Extract every `#[contractevent]` struct with its topic/payload layout.

    Returns one dict per event:
        {"struct": "StreamCreated", "name": "stream_created",
         "topics": ["stream_id", "sender", "recipient"],
         "payload": ["token", "deposited", ...]}

    A field is a topic iff it carries `#[topic]` *directly* above it. `pub` is
    required on the field so that helper methods (`impl` blocks) are never
    mistaken for fields.
    """
    source = strip_comments(source)
    events: list[dict] = []

    for match in re.finditer(r"#\[contractevent\]\s*pub struct (\w+)\s*\{", source):
        struct_name = match.group(1)
        start = match.end()
        end = source.find("\n}", start)
        if end == -1:
            raise CheckError(f"unterminated struct `{struct_name}` in events.rs")
        body = source[start:end]

        topics: list[str] = []
        payload: list[str] = []
        pending_topic = False
        for line in body.splitlines():
            stripped = line.strip()
            if stripped == "#[topic]":
                pending_topic = True
                continue
            # A non-attribute, non-empty line resets the annotation: only an
            # immediately-adjacent `#[topic]` marks the next field.
            if stripped and not stripped.startswith("#["):
                field = re.match(r"pub (\w+)\s*:", stripped)
                if field:
                    (topics if pending_topic else payload).append(field.group(1))
                pending_topic = False

        events.append(
            {
                "struct": struct_name,
                "name": snake_case(struct_name),
                "topics": topics,
                "payload": payload,
            }
        )

    if not events:
        raise CheckError("no #[contractevent] structs found in events.rs")
    return events


def extract_public_constants(source: str) -> dict[str, str]:
    """Extract `pub const NAME: TYPE = VALUE;` from a Rust source file."""
    source = strip_comments(source)
    consts: dict[str, str] = {}
    pattern = r"pub\s+const\s+(\w+)\s*:\s*[^=]+?=\s*([^;]+);"
    for match in re.finditer(pattern, source):
        consts[match.group(1)] = re.sub(r"\s+", "", match.group(2))
    return consts


# ---------------------------------------------------------------------------
# Document extraction
# ---------------------------------------------------------------------------


def _split_cell(cell: str) -> list[str]:
    """Split a table cell into field names, ignoring the em-dash 'no payload'."""
    cell = cell.strip()
    if cell in ("—", "-", "–", ""):
        return []
    return [p.strip().strip("`").strip() for p in cell.split(",") if p.strip()]


def parse_abi_event_table(document: str) -> tuple[dict[str, dict], list[str]]:
    """Parse the `## Events` table into {event: {topics, payload}}.

    Returns the parsed rows plus human-readable problems with the table's
    structure, such as prose spliced into the middle of it.
    """
    problems: list[str] = []
    rows: dict[str, dict] = {}

    # Isolate the Events section so a similarly-shaped table elsewhere in the
    # document cannot be picked up by mistake.
    start = document.find("\n## Events")
    if start == -1:
        raise CheckError("docs/ABI.md has no `## Events` section")
    end = document.find("\n## ", start + 1)
    section = document[start : end if end != -1 else len(document)]

    in_table = False
    # Prose appearing after at least one row. It is only a *problem* if further
    # table rows follow it, which means the table was split in two and the
    # later rows render as a separate headerless table. Prose after the final
    # row is ordinary commentary and is fine.
    pending_prose: str | None = None
    for raw in section.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("|"):
            if in_table and rows and pending_prose is None:
                pending_prose = line
            in_table = False
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(c and set(c) <= set("-: ") for c in cells):
            in_table = True  # separator row marks the table as started
            continue
        if not in_table and pending_prose is None:
            continue

        if len(cells) < 3:
            problems.append(f"events row has {len(cells)} cells, expected 3: {line!r}")
            continue

        name = cells[0].strip("` ")
        if not name or name == "event":
            continue  # header row

        if pending_prose is not None:
            problems.append(
                f"prose splits the events table before the `{name}` row: "
                f"{pending_prose[:60]!r}"
            )
            pending_prose = None

        rows[name] = {
            "topics": _split_cell(cells[1]),
            "payload": _split_cell(cells[2]),
        }

    return rows, problems


def abi_declares_frozen(document: str) -> bool:
    """True when the document still claims to be the frozen interface."""
    return bool(re.search(r"\*\*Status:\s*FROZEN", document, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    if not path.exists():
        raise CheckError(f"required file not found: {path}")
    return path.read_text(encoding="utf-8")


def check_event_layout(source_events: list[dict], doc_rows: dict[str, dict]) -> list[str]:
    """Every event's topic order and payload order must match the document."""
    findings: list[str] = []
    source_names = {e["name"] for e in source_events}

    for event in source_events:
        name = event["name"]
        row = doc_rows.get(name)
        if row is None:
            findings.append(
                f"event `{name}` exists in events.rs but is not documented in "
                "the docs/ABI.md Events table (it is part of the ABI)"
            )
            continue

        if row["topics"] != event["topics"]:
            findings.append(
                f"event `{name}` topic layout drifted.\n"
                f"    source:      {event['topics']}\n"
                f"    docs/ABI.md: {row['topics']}\n"
                "    Re-topicking or reordering a topic is a BREAKING change "
                "under the freeze rules."
            )

        if row["payload"] != event["payload"]:
            findings.append(
                f"event `{name}` payload layout drifted.\n"
                f"    source:      {event['payload']}\n"
                f"    docs/ABI.md: {row['payload']}\n"
                "    Reordering or removing a payload field is a BREAKING change."
            )

    for name in doc_rows:
        if name not in source_names:
            findings.append(
                f"docs/ABI.md documents event `{name}`, which no longer exists "
                "in events.rs (remove the row or restore the struct)"
            )

    return findings


def check_constants(source_consts: dict[str, str], document: str) -> list[str]:
    """Documented constant values must match the source."""
    findings: list[str] = []
    for name, source_value in source_consts.items():
        match = re.search(rf"`{re.escape(name)}\s*=\s*([^`]+)`", document)
        if not match:
            # Not every constant belongs in the ABI document; only compare the
            # ones the document actually quotes.
            continue
        doc_value = re.sub(r"\s+", "", match.group(1))
        if doc_value != source_value:
            findings.append(
                f"constant `{name}` drifted: source={source_value}, "
                f"docs/ABI.md={doc_value}"
            )
    return findings


def check_freeze_status(document: str) -> list[str]:
    if abi_declares_frozen(document):
        return []
    return [
        "docs/ABI.md no longer declares a FROZEN status. The interface of "
        "record must state its freeze; lifting the freeze is a deliberate, "
        "reviewed decision, not a side effect."
    ]


def run_checks() -> tuple[list[str], list[str]]:
    """Return (findings, structural_problems). Either non-empty means failure."""
    source_events = extract_events(_read(EVENTS_RS))
    doc_rows, table_problems = parse_abi_event_table(_read(ABI_MD))
    source_consts = extract_public_constants(_read(LIB_RS))

    findings: list[str] = []
    findings.extend(check_event_layout(source_events, doc_rows))
    findings.extend(check_constants(source_consts, _read(ABI_MD)))
    findings.extend(check_freeze_status(_read(ABI_MD)))
    return findings, table_problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    args = parser.parse_args(argv)

    try:
        findings, problems = run_checks()
    except CheckError as exc:
        print(f"abi-drift-check: could not run: {exc}", file=sys.stderr)
        return 2

    ok = not (findings or problems)

    if args.json:
        print(json.dumps({"ok": ok, "findings": findings, "problems": problems}, indent=2))
        return 0 if ok else 1

    if findings or problems:
        for problem in problems:
            print(f"docs/ABI.md table structure: {problem}")
        if findings:
            print(f"ABI drift detected ({len(findings)} finding(s)):\n")
            for i, finding in enumerate(findings, 1):
                print(f"{i}. {finding}")
        print(
            "\ndocs/ABI.md is the frozen interface of record. Restore the source "
            "or update the document deliberately, in the same commit."
        )
        return 1

    print("OK: docs/ABI.md matches the interface declared in source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
