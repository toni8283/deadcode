"""
state.py – deterministic candidate ID management and scan-state persistence.

Design
------
IDs (DC-001, DC-002, …) must be:
  1. Deterministic for the same repository state.
  2. Stable across runs when the source does not change.
  3. Mappable back to a Candidate without keeping a live process.

Strategy
--------
We sort all candidates by ``(qualified_name, file, line)`` and assign
sequential IDs.  The sort key is fully deterministic given the same
analysis output.

State file
----------
After ``deadcode scan``, a JSON state file is written to:

    <repo_root>/.deadcode/state.json

This file contains:
  - The repo root path.
  - The scan timestamp.
  - All candidates serialised as dicts, keyed by their DC-NNN id.
  - Any proven candidates (by DC-NNN id) referencing their ProofResult.

The file is small (no source code, no secrets).  It should be added to
``.gitignore`` to avoid committing transient scan state.

All operations on the state file are atomic: we write to a temp file then
rename it into place.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deadcode.models import (
    Candidate,
    EvidenceItem,
    Location,
    SafetyClassification,
    SymbolDef,
    SymbolKind,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_DIR = ".deadcode"
STATE_FILE = "state.json"
STATE_VERSION = 1

# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def assign_ids(candidates: list[Candidate]) -> dict[str, Candidate]:
    """
    Sort *candidates* deterministically and return an ordered mapping of
    DC-NNN → Candidate.

    Sort key: (classification_order, qualified_name, file, line)
    so PROVABLE candidates always come first in the ID sequence.
    """
    _class_order = {
        SafetyClassification.PROVABLE: 0,
        SafetyClassification.REVIEW: 1,
        SafetyClassification.ACTIVE: 2,
    }

    sorted_cands = sorted(
        candidates,
        key=lambda c: (
            _class_order.get(c.classification, 9),
            c.qualified_name,
            c.file,
            c.line,
        ),
    )

    result: dict[str, Candidate] = {}
    for i, cand in enumerate(sorted_cands, start=1):
        dc_id = f"DC-{i:03d}"
        result[dc_id] = cand
    return result


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _candidate_to_dict(dc_id: str, cand: Candidate) -> dict[str, Any]:
    """Serialise one Candidate plus its assigned ID to a plain dict."""
    return {
        "id": dc_id,
        "qualified_name": cand.qualified_name,
        "name": cand.symbol.name,
        "kind": cand.symbol.kind.value,
        "file": cand.file,
        "line": cand.line,
        "classification": cand.classification.value,
        "ref_count": cand.ref_count,
        "test_ref_count": cand.test_ref_count,
        "ref_locations": cand.ref_locations,
        "import_relationships": cand.import_relationships,
        "is_exported": cand.is_exported,
        "uncertainty_reasons": cand.uncertainty_reasons,
        "is_private": cand.symbol.is_private,
        "has_decorator": cand.symbol.has_decorator,
        "is_dunder": cand.symbol.is_dunder,
        "in_all": cand.symbol.in_all,
        "evidence": [
            {"reason": e.reason, "detail": e.detail}
            for e in cand.evidence
        ],
    }


def _candidate_from_dict(d: dict[str, Any]) -> tuple[str, Candidate]:
    """
    Deserialise a candidate dict back to a (dc_id, Candidate) pair.

    This reconstructs enough of the Candidate structure for the CLI
    to display and drive further operations.  The ``extra`` field and
    some secondary fields are not preserved (they are not needed post-scan).
    """
    dc_id: str = d["id"]

    defn = SymbolDef(
        name=d["name"],
        qualified_name=d["qualified_name"],
        kind=SymbolKind(d["kind"]),
        location=Location(file=d["file"], line=d["line"]),
        is_private=d.get("is_private", False),
        in_all=d.get("in_all"),
        has_decorator=d.get("has_decorator", False),
        is_dunder=d.get("is_dunder", False),
    )

    evidence = [
        EvidenceItem(reason=e["reason"], detail=e.get("detail", ""))
        for e in d.get("evidence", [])
    ]

    cand = Candidate(
        symbol=defn,
        classification=SafetyClassification(d["classification"]),
        evidence=evidence,
        ref_count=d.get("ref_count", 0),
        test_ref_count=d.get("test_ref_count", 0),
        ref_locations=d.get("ref_locations", []),
        import_relationships=d.get("import_relationships", []),
        is_exported=d.get("is_exported"),
        uncertainty_reasons=d.get("uncertainty_reasons", []),
    )
    return dc_id, cand


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------


def state_path(repo_root: Path) -> Path:
    return repo_root / STATE_DIR / STATE_FILE


def save_state(
    repo_root: Path,
    id_map: dict[str, Candidate],
    proven: dict[str, dict[str, Any]] | None = None,
    files_scanned: int = 0,
    total_definitions: int = 0,
) -> Path:
    """
    Write the current scan state to ``<repo_root>/.deadcode/state.json``.

    Parameters
    ----------
    repo_root:
        Absolute repository root.
    id_map:
        DC-NNN → Candidate mapping produced by ``assign_ids()``.
    proven:
        Optional map of DC-NNN → proof result dict from previous prove runs.
        If None, any existing proven entries in the state file are preserved.
    files_scanned, total_definitions:
        Summary counts to record in the state.

    Returns
    -------
    Path
        The path to the written state file.
    """
    state_dir = repo_root / STATE_DIR
    state_dir.mkdir(exist_ok=True)

    # Load existing proven entries if not overriding
    existing_proven: dict[str, Any] = {}
    sf = state_dir / STATE_FILE
    if sf.exists() and proven is None:
        try:
            existing = json.loads(sf.read_text(encoding="utf-8"))
            existing_proven = existing.get("proven", {})
        except Exception:  # noqa: BLE001
            pass

    proven_to_save = proven if proven is not None else existing_proven

    state: dict[str, Any] = {
        "version": STATE_VERSION,
        "repo_root": str(repo_root),
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "files_scanned": files_scanned,
        "total_definitions": total_definitions,
        "candidates": {
            dc_id: _candidate_to_dict(dc_id, cand)
            for dc_id, cand in id_map.items()
        },
        "proven": proven_to_save,
    }

    # Atomic write
    sf_parent = sf.parent
    sf_parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(sf_parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, str(sf))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return sf


def load_state(repo_root: Path) -> dict[str, Any] | None:
    """
    Load the state file from ``<repo_root>/.deadcode/state.json``.

    Returns None if the file does not exist or cannot be parsed.
    """
    sf = state_path(repo_root)
    if not sf.exists():
        return None
    try:
        return json.loads(sf.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def load_candidate(repo_root: Path, dc_id: str) -> Candidate | None:
    """
    Load and deserialise a single candidate by ID from the state file.

    Returns None if the state file does not exist or the ID is not found.
    """
    state = load_state(repo_root)
    if state is None:
        return None
    candidates_raw = state.get("candidates", {})
    raw = candidates_raw.get(dc_id)
    if raw is None:
        return None
    _, cand = _candidate_from_dict(raw)
    return cand


def load_all_candidates(repo_root: Path) -> dict[str, Candidate] | None:
    """
    Load all candidates from the state file.

    Returns None if the state file does not exist.
    Returns a dict of DC-NNN → Candidate on success.
    """
    state = load_state(repo_root)
    if state is None:
        return None
    result: dict[str, Candidate] = {}
    for raw in state.get("candidates", {}).values():
        dc_id, cand = _candidate_from_dict(raw)
        result[dc_id] = cand
    return result


def record_proof(
    repo_root: Path,
    dc_id: str,
    proof_dict: dict[str, Any],
) -> None:
    """
    Persist a proof result for *dc_id* in the state file.

    Merges with existing state rather than overwriting the whole file.
    """
    state = load_state(repo_root) or {}
    proven: dict[str, Any] = state.get("proven", {})
    proven[dc_id] = proof_dict

    state_dir = repo_root / STATE_DIR
    state_dir.mkdir(exist_ok=True)
    sf = state_dir / STATE_FILE

    state["proven"] = proven

    fd, tmp = tempfile.mkstemp(dir=str(state_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, str(sf))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_proof(repo_root: Path, dc_id: str) -> dict[str, Any] | None:
    """Return the stored proof result for *dc_id*, or None."""
    state = load_state(repo_root)
    if state is None:
        return None
    return state.get("proven", {}).get(dc_id)


def is_proven(repo_root: Path, dc_id: str) -> bool:
    """Return True if *dc_id* has a stored proof with status PROVEN."""
    proof = get_proof(repo_root, dc_id)
    if proof is None:
        return False
    return proof.get("status") == "PROVEN"
