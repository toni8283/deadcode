"""
proof_models.py – structured result types for the proof engine.

All dataclasses are JSON-serialisable via ``dataclasses.asdict``.

These types are kept entirely separate from the analysis-layer models so that
the proof engine can be tested, imported, and reasoned about independently.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Status enumerations
# ---------------------------------------------------------------------------


class ProofStatus(str, enum.Enum):
    """
    Top-level outcome of a proof attempt.

    PROVEN
        The candidate was removed in an isolated worktree, all detected
        verification checks passed, and re-analysis confirmed the symbol
        is gone.  The user's working tree is unchanged.

    FAILED
        The proof attempt ran but a required step failed (verification
        command non-zero exit, removal error, re-analysis found the symbol
        still present, or an unexpected exception occurred).

    BLOCKED
        The proof could not be attempted because a precondition was not
        met.  Common reasons: the candidate is classified REVIEW rather
        than PROVABLE; the repository is not a Git repo; Git command
        unavailable; the candidate is a symbol kind not yet supported for
        removal.
    """

    PROVEN = "PROVEN"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class VerificationStatus(str, enum.Enum):
    """
    Outcome of a single verification step (tests, typecheck, build).

    PASSED   – command ran and exited 0.
    FAILED   – command ran and exited non-zero.
    SKIPPED  – command was not configured / tool not available.
    NOT_RUN  – step was never attempted (e.g. a prior step already failed).
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    NOT_RUN = "NOT_RUN"


class RemovalStatus(str, enum.Enum):
    """Outcome of the candidate-removal step inside the isolated worktree."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


# ---------------------------------------------------------------------------
# Per-step verification result
# ---------------------------------------------------------------------------


@dataclass
class VerificationResult:
    """
    Result of running one verification command inside the isolated worktree.

    Attributes
    ----------
    name:
        Human-readable label, e.g. ``"pytest"``, ``"mypy"``, ``"build"``.
    status:
        One of the :class:`VerificationStatus` values.
    command:
        The exact command that was (or would have been) run, as a list of
        strings.  Empty list when status is SKIPPED or NOT_RUN.
    exit_code:
        Process exit code, or ``None`` when not run / skipped.
    stdout:
        Captured standard output (truncated to avoid huge payloads).
    stderr:
        Captured standard error (truncated to avoid huge payloads).
    duration_seconds:
        Wall-clock seconds the command took, or ``None``.
    reason:
        Human-readable explanation for SKIPPED or FAILED status.
    """

    name: str
    status: VerificationStatus
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Removal result
# ---------------------------------------------------------------------------


@dataclass
class RemovalResult:
    """
    Records what happened when the proof engine attempted to remove the
    candidate definition inside the isolated worktree.

    Attributes
    ----------
    status:
        SUCCESS / FAILED / NOT_ATTEMPTED.
    file_path:
        Path to the file that was modified (inside the worktree), or empty.
    lines_removed:
        Number of source lines that were deleted.
    reason:
        Human-readable explanation when status != SUCCESS.
    """

    status: RemovalStatus = RemovalStatus.NOT_ATTEMPTED
    file_path: str = ""
    lines_removed: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# Re-analysis result
# ---------------------------------------------------------------------------


@dataclass
class ReanalysisResult:
    """
    Summary of re-running the DeadCode analyzer on the isolated worktree
    after candidate removal.

    Attributes
    ----------
    candidate_absent:
        ``True`` if the target symbol no longer appears in the analysis
        results (i.e. it was completely removed and not re-introduced).
    total_candidates_after:
        Total number of candidates in the post-removal analysis.
    error:
        Non-empty if the re-analysis itself failed (e.g. parse error in the
        modified file).
    """

    candidate_absent: bool = False
    total_candidates_after: int = 0
    error: str = ""


# ---------------------------------------------------------------------------
# Top-level proof result
# ---------------------------------------------------------------------------


@dataclass
class ProofResult:
    """
    Complete result of a proof attempt for one candidate.

    This is the primary type returned by ``prove_candidate()``.

    JSON-serialisable via ``dataclasses.asdict(result)``.

    Attributes
    ----------
    candidate_qualified_name:
        Fully qualified name of the candidate that was probed, e.g.
        ``mypackage.utils.calculate_total``.
    candidate_file:
        Path of the source file that defines the candidate (relative to repo
        root for portability).
    candidate_line:
        Line number of the definition.
    status:
        Top-level :class:`ProofStatus`.
    removal:
        Details of the removal step.
    verifications:
        Ordered list of verification step results.
    reanalysis:
        Result of re-running DeadCode after removal.
    worktree_id:
        A short opaque identifier for the worktree (e.g. the temp directory
        basename).  Not a full path – avoids leaking system temp locations.
    duration_seconds:
        Total wall-clock time for the entire proof attempt.
    failure_reason:
        Human-readable explanation when status is FAILED or BLOCKED.
    """

    candidate_qualified_name: str
    candidate_file: str
    candidate_line: int
    status: ProofStatus
    removal: RemovalResult = field(default_factory=RemovalResult)
    verifications: list[VerificationResult] = field(default_factory=list)
    reanalysis: ReanalysisResult = field(default_factory=ReanalysisResult)
    worktree_id: str = ""
    duration_seconds: float = 0.0
    failure_reason: str = ""

    # Convenience accessors
    @property
    def test_result(self) -> VerificationResult | None:
        return next((v for v in self.verifications if v.name == "pytest"), None)

    @property
    def typecheck_result(self) -> VerificationResult | None:
        return next((v for v in self.verifications if v.name == "mypy"), None)

    @property
    def build_result(self) -> VerificationResult | None:
        return next((v for v in self.verifications if v.name == "build"), None)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation."""
        import dataclasses
        return dataclasses.asdict(self)
