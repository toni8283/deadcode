"""
DeadCode – deterministic static-analysis engine for unused Python code.

Analysis API
------------
    from deadcode import analyze_repo, AnalysisResult

    result = analyze_repo("/path/to/project")
    for candidate in result.candidates:
        print(candidate.qualified_name, candidate.classification)

Proof API
---------
    from deadcode import prove_candidate, ProofResult, ProofStatus

    result = prove_candidate("/path/to/repo", candidate)
    if result.status == ProofStatus.PROVEN:
        print(f"{candidate.qualified_name} is safe to remove")
"""

from deadcode.analyzer import analyze_repo
from deadcode.models import AnalysisResult, Candidate, SafetyClassification
from deadcode.proof_models import ProofResult, ProofStatus, VerificationStatus
from deadcode.prover import ProofOptions, prove_candidate

__all__ = [
    # Analysis
    "analyze_repo",
    "AnalysisResult",
    "Candidate",
    "SafetyClassification",
    # Proof
    "prove_candidate",
    "ProofResult",
    "ProofStatus",
    "ProofOptions",
    "VerificationStatus",
]
