"""
DeadCode – deterministic static-analysis engine for unused Python code.

Public API
----------
    from deadcode import analyze_repo, AnalysisResult

    result = analyze_repo("/path/to/project")
    for candidate in result.candidates:
        print(candidate.qualified_name, candidate.classification)
"""

from deadcode.analyzer import analyze_repo
from deadcode.models import AnalysisResult, Candidate, SafetyClassification

__all__ = ["analyze_repo", "AnalysisResult", "Candidate", "SafetyClassification"]
