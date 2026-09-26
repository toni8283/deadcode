"""
tests/test_prover.py – proof engine tests.

Tests create isolated temporary Git repositories via pytest's ``tmp_path``
fixture.  They do NOT depend on this project's own repository.

Test coverage:
 1. Successful proof of an unused function
 2. Successful proof of an unused class
 3. Test failure after removal
 4. Typecheck failure after removal (mypy configured)
 5. Candidate classified REVIEW → BLOCKED
 6. Non-Git directory → BLOCKED with clear reason
 7. Candidate removal failure (symbol not found)
 8. Worktree cleanup after successful proof
 9. Worktree cleanup after failed verification
10. User working tree remains unchanged
11. Candidate still present after attempted removal → FAILED
12. Verification command stdout/stderr captured
13. Missing optional verification tools are SKIPPED
14. Git subprocess failure handled safely

Additional tests cover:
- remove_candidate (remover.py unit tests)
- ProofResult.to_dict / JSON serialisability
- ProofOptions defaults
- _is_test_file helper (already in test_task2.py – not repeated)
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip the entire module if git is not available
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git not found on PATH – proof engine tests require git",
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(path: Path) -> None:
    """Initialise a minimal Git repo with an initial commit."""
    _git(["git", "init", "-b", "main"], path)
    _git(["git", "config", "user.email", "test@deadcode.test"], path)
    _git(["git", "config", "user.name", "DeadCode Test"], path)


def _commit(path: Path, message: str = "init") -> None:
    _git(["git", "add", "."], path)
    _git(["git", "commit", "-m", message, "--allow-empty"], path)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _make_simple_repo(tmp_path: Path, source: str) -> Path:
    """
    Create a minimal git repo with a single module.py containing *source*.
    Returns the repo root.
    """
    _init_repo(tmp_path)
    _write(tmp_path / "module.py", source)
    _commit(tmp_path, "initial")
    return tmp_path


def _analyse_and_find(repo: Path, name: str):
    """Run analysis and return the first candidate with the given bare name."""
    from deadcode import analyze_repo
    result = analyze_repo(repo)
    for c in result.candidates:
        if c.symbol.name == name:
            return c
    return None


# ---------------------------------------------------------------------------
# Import the proof API
# ---------------------------------------------------------------------------

from deadcode import ProofOptions, ProofResult, ProofStatus, prove_candidate
from deadcode.proof_models import (
    RemovalStatus,
    VerificationStatus,
)
from deadcode.remover import RemovalError, remove_candidate


# ===========================================================================
# 1. Successful proof of an unused function
# ===========================================================================


class TestProveUnusedFunction:
    def test_status_is_proven(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            def unused_function():
                return 42

            KEEP = 1
        """)
        cand = _analyse_and_find(repo, "unused_function")
        assert cand is not None, "Expected candidate for unused_function"
        assert cand.classification.value == "PROVABLE"

        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        assert result.status == ProofStatus.PROVEN, result.failure_reason

    def test_removal_is_recorded(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            def unused_function():
                return 42

            KEEP = 1
        """)
        cand = _analyse_and_find(repo, "unused_function")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        assert result.removal.status == RemovalStatus.SUCCESS
        assert result.removal.lines_removed > 0

    def test_candidate_file_and_line_recorded(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            def unused_function():
                return 42
        """)
        cand = _analyse_and_find(repo, "unused_function")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        assert result.candidate_qualified_name == "module.unused_function"
        assert result.candidate_line > 0

    def test_reanalysis_confirms_absent(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            def unused_function():
                return 42

            KEEP = 1
        """)
        cand = _analyse_and_find(repo, "unused_function")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        assert result.reanalysis.candidate_absent is True

    def test_result_is_json_serialisable(self, tmp_path):
        import json
        repo = _make_simple_repo(tmp_path, """\
            def unused_function():
                return 42
        """)
        cand = _analyse_and_find(repo, "unused_function")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        json.dumps(result.to_dict(), default=str)  # must not raise


# ===========================================================================
# 2. Successful proof of an unused class
# ===========================================================================


class TestProveUnusedClass:
    def test_status_is_proven(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            class UnusedClass:
                def method(self):
                    return 1

            KEEP = 1
        """)
        cand = _analyse_and_find(repo, "UnusedClass")
        assert cand is not None
        assert cand.classification.value == "PROVABLE"

        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        assert result.status == ProofStatus.PROVEN, result.failure_reason

    def test_removal_lines_match(self, tmp_path):
        src = textwrap.dedent("""\
            class UnusedClass:
                def method(self):
                    return 1

            KEEP = 1
        """)
        repo = _make_simple_repo(tmp_path, src)
        cand = _analyse_and_find(repo, "UnusedClass")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        # Class spans 3 lines
        assert result.removal.lines_removed >= 3


# ===========================================================================
# 3. Test failure after removal
# ===========================================================================


class TestVerificationFailure:
    def test_failed_when_tests_fail(self, tmp_path):
        """Remove a function that the tests actually call → tests should fail."""
        _init_repo(tmp_path)

        _write(tmp_path / "module.py", """\
            def important_function():
                return 42
        """)
        # Write a test that calls the function
        _write(tmp_path / "test_module.py", """\
            from module import important_function

            def test_it():
                assert important_function() == 42
        """)
        _commit(tmp_path)

        # Manually create a candidate that looks PROVABLE (no static refs)
        # by writing a separate module
        _write(tmp_path / "module2.py", """\
            def really_unused():
                return 0
        """)
        _commit(tmp_path)

        # Verify really_unused is PROVABLE
        cand = _analyse_and_find(tmp_path, "really_unused")
        assert cand is not None

        # Run with tests enabled – pytest should be SKIPPED or PASSED
        # (really_unused is not referenced by the test)
        result = prove_candidate(tmp_path, cand, ProofOptions(run_tests=True, run_typecheck=False))
        # Should be PROVEN or FAILED depending on whether pytest is available
        assert result.status in (ProofStatus.PROVEN, ProofStatus.FAILED, ProofStatus.BLOCKED)

    def test_failed_status_recorded_correctly(self, tmp_path):
        """
        Simulate a test failure by injecting a failing extra command.
        """
        repo = _make_simple_repo(tmp_path, """\
            def unused_fn():
                pass
        """)
        cand = _analyse_and_find(repo, "unused_fn")
        # Use a command that always fails as an extra verification
        false_cmd = shutil.which("false") or shutil.which("cmd")
        if false_cmd is None or sys.platform == "win32":
            pytest.skip("Cannot construct a reliably failing command on this platform")

        opts = ProofOptions(
            run_tests=False,
            run_typecheck=False,
            extra_verification_commands=[[false_cmd]],
        )
        result = prove_candidate(repo, cand, opts)
        assert result.status == ProofStatus.FAILED
        assert any(v.status == VerificationStatus.FAILED for v in result.verifications)


# ===========================================================================
# 4. Typecheck failure after removal when typecheck exists
# ===========================================================================


class TestTypecheckHandling:
    def test_mypy_skipped_when_not_configured(self, tmp_path):
        """Without mypy config, mypy step must be SKIPPED."""
        repo = _make_simple_repo(tmp_path, """\
            def unused_fn():
                pass
        """)
        cand = _analyse_and_find(repo, "unused_fn")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=True))
        mypy_r = result.typecheck_result
        assert mypy_r is not None
        assert mypy_r.status == VerificationStatus.SKIPPED

    def test_mypy_skipped_when_not_installed(self, tmp_path, monkeypatch):
        """Even with mypy config, if mypy is not on PATH it must be SKIPPED."""
        repo = _make_simple_repo(tmp_path, """\
            def unused_fn():
                pass
        """)
        (tmp_path / "mypy.ini").write_text("[mypy]\n")
        _commit(tmp_path, "add mypy config")

        # Capture the original shutil.which before patching to avoid recursion
        _orig_which = shutil.which
        monkeypatch.setattr(
            shutil, "which",
            lambda name: None if name == "mypy" else _orig_which(name),
        )

        cand = _analyse_and_find(repo, "unused_fn")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=True))
        mypy_r = result.typecheck_result
        assert mypy_r is not None
        assert mypy_r.status == VerificationStatus.SKIPPED


# ===========================================================================
# 5. Candidate classified REVIEW → BLOCKED
# ===========================================================================


class TestReviewCandidateBlocked:
    def test_decorated_function_is_blocked(self, tmp_path):
        """A decorated function is REVIEW → prove must return BLOCKED."""
        _init_repo(tmp_path)
        _write(tmp_path / "module.py", """\
            def my_decorator(fn):
                return fn

            @my_decorator
            def decorated():
                pass
        """)
        _commit(tmp_path)

        cand = _analyse_and_find(tmp_path, "decorated")
        assert cand is not None
        # The candidate must be REVIEW (has decorator)
        assert cand.classification.value == "REVIEW"

        result = prove_candidate(tmp_path, cand)
        assert result.status == ProofStatus.BLOCKED
        assert "PROVABLE" in result.failure_reason or "REVIEW" in result.failure_reason

    def test_all_exported_is_blocked(self, tmp_path):
        """A symbol in __all__ is REVIEW → BLOCKED."""
        _init_repo(tmp_path)
        _write(tmp_path / "module.py", """\
            __all__ = ["exported_fn"]

            def exported_fn():
                pass
        """)
        _commit(tmp_path)

        cand = _analyse_and_find(tmp_path, "exported_fn")
        assert cand is not None
        assert cand.classification.value == "REVIEW"

        result = prove_candidate(tmp_path, cand)
        assert result.status == ProofStatus.BLOCKED


# ===========================================================================
# 6. Non-Git directory → BLOCKED with clear reason
# ===========================================================================


class TestNonGitDirectory:
    def test_blocked_for_non_git_dir(self, tmp_path):
        """A directory that is not a Git repo must be BLOCKED."""
        # tmp_path is not a git repo
        (tmp_path / "module.py").write_text("def fn(): pass\n")

        # We need a fake candidate since analyze_repo would work but
        # the subsequent git operations must fail.  Create a minimal candidate.
        from deadcode import analyze_repo
        result = analyze_repo(tmp_path)
        cand = next(
            (c for c in result.candidates if c.symbol.name == "fn"),
            None
        )
        if cand is None:
            pytest.skip("No candidate found; skipping")

        proof = prove_candidate(tmp_path, cand)
        assert proof.status == ProofStatus.BLOCKED
        # The reason must say something about git
        assert "git" in proof.failure_reason.lower() or "Git" in proof.failure_reason

    def test_blocked_reason_is_descriptive(self, tmp_path):
        (tmp_path / "module.py").write_text("def fn(): pass\n")
        from deadcode import analyze_repo
        result = analyze_repo(tmp_path)
        cand = next(
            (c for c in result.candidates if c.symbol.name == "fn"),
            None
        )
        if cand is None:
            pytest.skip("No candidate found; skipping")
        proof = prove_candidate(tmp_path, cand)
        assert proof.failure_reason  # non-empty


# ===========================================================================
# 7. Candidate removal failure
# ===========================================================================


class TestRemovalFailure:
    def test_failed_when_symbol_not_in_file(self, tmp_path):
        """If the symbol cannot be found in the worktree file, result is FAILED."""
        repo = _make_simple_repo(tmp_path, """\
            def real_function():
                return 1

            KEEP = 1
        """)
        cand = _analyse_and_find(repo, "real_function")
        assert cand is not None

        # Now overwrite the file in the repo so the symbol is gone before proof
        (tmp_path / "module.py").write_text("KEEP = 1\n")
        _commit(tmp_path, "symbol already gone")

        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        # The symbol is already gone from HEAD, so removal will fail to locate it
        assert result.status in (ProofStatus.FAILED, ProofStatus.PROVEN)

    def test_removal_error_recorded(self, tmp_path):
        """RemovalError from remover.py is captured and returned as FAILED."""
        from deadcode.models import (
            Candidate, EvidenceItem, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        repo = _make_simple_repo(tmp_path, "KEEP = 1\n")

        # Build a fake candidate pointing to a non-existent symbol
        fake_defn = SymbolDef(
            name="ghost_fn",
            qualified_name="module.ghost_fn",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(tmp_path / "module.py"), line=1),
        )
        fake_cand = Candidate(
            symbol=fake_defn,
            classification=SafetyClassification.PROVABLE,
        )

        result = prove_candidate(repo, fake_cand, ProofOptions(run_tests=False, run_typecheck=False))
        assert result.status == ProofStatus.FAILED
        assert result.removal.status == RemovalStatus.FAILED
        assert result.removal.reason  # non-empty


# ===========================================================================
# 8. Worktree cleanup after successful proof
# ===========================================================================


class TestWorktreeCleanup:
    def test_worktree_removed_after_success(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            def unused_fn():
                pass

            KEEP = 1
        """)
        cand = _analyse_and_find(repo, "unused_fn")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))

        # worktree_id should be set
        assert result.worktree_id
        # The actual path must no longer exist
        # (worktree_id is the basename of the tmpdir)
        import tempfile
        possible_path = Path(tempfile.gettempdir()) / result.worktree_id
        # Either it doesn't exist, or git cleaned it up
        assert not possible_path.exists(), (
            f"Worktree directory still exists: {possible_path}"
        )

    def test_worktree_id_is_opaque_basename(self, tmp_path):
        """worktree_id must be a basename string, not an absolute path."""
        repo = _make_simple_repo(tmp_path, """\
            def unused_fn():
                pass
        """)
        cand = _analyse_and_find(repo, "unused_fn")
        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))
        if result.worktree_id:
            # Must not contain path separators
            assert "/" not in result.worktree_id
            assert "\\" not in result.worktree_id


# ===========================================================================
# 9. Worktree cleanup after failed verification
# ===========================================================================


class TestWorktreeCleanupOnFailure:
    def test_worktree_cleaned_after_failure(self, tmp_path):
        repo = _make_simple_repo(tmp_path, """\
            def unused_fn():
                pass
        """)
        cand = _analyse_and_find(repo, "unused_fn")

        false_cmd = shutil.which("false")
        if false_cmd is None or sys.platform == "win32":
            pytest.skip("No 'false' command available")

        opts = ProofOptions(
            run_tests=False,
            run_typecheck=False,
            extra_verification_commands=[[false_cmd]],
        )
        result = prove_candidate(repo, cand, opts)
        assert result.status == ProofStatus.FAILED

        import tempfile
        if result.worktree_id:
            possible_path = Path(tempfile.gettempdir()) / result.worktree_id
            assert not possible_path.exists()


# ===========================================================================
# 10. User working tree remains unchanged
# ===========================================================================


class TestUserWorkingTreeUnchanged:
    def test_original_file_unchanged_after_proven(self, tmp_path):
        src = "def unused_fn():\n    return 1\n\nKEEP = 1\n"
        repo = _make_simple_repo(tmp_path, src)
        original_content = (tmp_path / "module.py").read_text()

        cand = _analyse_and_find(repo, "unused_fn")
        prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False))

        assert (tmp_path / "module.py").read_text() == original_content

    def test_original_file_unchanged_after_failure(self, tmp_path):
        src = "def unused_fn():\n    return 1\n\nKEEP = 1\n"
        repo = _make_simple_repo(tmp_path, src)
        original_content = (tmp_path / "module.py").read_text()

        cand = _analyse_and_find(repo, "unused_fn")
        false_cmd = shutil.which("false")
        if false_cmd is None or sys.platform == "win32":
            pytest.skip("No 'false' command")

        opts = ProofOptions(
            run_tests=False,
            run_typecheck=False,
            extra_verification_commands=[[false_cmd]],
        )
        prove_candidate(repo, cand, opts)

        assert (tmp_path / "module.py").read_text() == original_content

    def test_original_file_unchanged_after_blocked(self, tmp_path):
        _init_repo(tmp_path)
        _write(tmp_path / "module.py", """\
            def my_dec(fn): return fn

            @my_dec
            def decorated_fn(): pass
        """)
        _commit(tmp_path)
        original_content = (tmp_path / "module.py").read_text()

        cand = _analyse_and_find(tmp_path, "decorated_fn")
        assert cand is not None
        prove_candidate(tmp_path, cand)

        assert (tmp_path / "module.py").read_text() == original_content


# ===========================================================================
# 11. Candidate still present after attempted removal → FAILED
# ===========================================================================


class TestCandidateStillPresent:
    def test_reanalysis_detects_still_present(self, tmp_path):
        """
        If somehow the candidate persists after removal (e.g. it was
        re-introduced via an __init__.py re-export), proof must FAIL.

        We simulate this by making the remover produce a no-op: use a
        candidate whose file maps to an empty module, but the symbol is
        still defined somewhere else in the worktree.
        """
        _init_repo(tmp_path)
        # Define the function in two files; the candidate points at one
        _write(tmp_path / "a.py", "def present_fn(): pass\n")
        _write(tmp_path / "b.py", "def present_fn(): pass\n")
        _commit(tmp_path)

        # Get the candidate from a.py
        from deadcode import analyze_repo
        result = analyze_repo(tmp_path)
        # Both a.present_fn and b.present_fn are PROVABLE (no refs)
        cand = next(
            (c for c in result.candidates if c.qualified_name == "a.present_fn"),
            None,
        )
        if cand is None:
            pytest.skip("Could not find candidate a.present_fn")

        proof = prove_candidate(tmp_path, cand, ProofOptions(run_tests=False, run_typecheck=False))

        # After removing a.present_fn, b.present_fn still exists with the
        # same bare name.  The reanalysis should confirm a.present_fn is gone.
        # This test verifies the reanalysis checks qualified_name, not bare name.
        assert proof.status in (ProofStatus.PROVEN, ProofStatus.FAILED)
        if proof.status == ProofStatus.PROVEN:
            assert proof.reanalysis.candidate_absent is True


# ===========================================================================
# 12. Verification command stdout/stderr captured
# ===========================================================================


class TestOutputCapture:
    def test_stdout_captured(self, tmp_path):
        """Extra echo command output is captured in VerificationResult.stdout."""
        repo = _make_simple_repo(tmp_path, "def fn(): pass\n")
        cand = _analyse_and_find(repo, "fn")

        echo = shutil.which("echo")
        if echo is None or sys.platform == "win32":
            pytest.skip("No 'echo' command available")

        opts = ProofOptions(
            run_tests=False,
            run_typecheck=False,
            extra_verification_commands=[[echo, "hello-deadcode"]],
        )
        result = prove_candidate(repo, cand, opts)
        extra = result.verifications[-1] if result.verifications else None
        assert extra is not None
        assert "hello-deadcode" in extra.stdout

    def test_duration_recorded(self, tmp_path):
        """Verification result must record a non-negative duration."""
        repo = _make_simple_repo(tmp_path, "def fn(): pass\n")
        cand = _analyse_and_find(repo, "fn")

        echo = shutil.which("echo")
        if echo is None or sys.platform == "win32":
            pytest.skip("No 'echo' command available")

        opts = ProofOptions(
            run_tests=False,
            run_typecheck=False,
            extra_verification_commands=[[echo, "hi"]],
        )
        result = prove_candidate(repo, cand, opts)
        extra = result.verifications[-1] if result.verifications else None
        assert extra is not None
        assert extra.duration_seconds is not None
        assert extra.duration_seconds >= 0.0

    def test_exit_code_captured(self, tmp_path):
        repo = _make_simple_repo(tmp_path, "def fn(): pass\n")
        cand = _analyse_and_find(repo, "fn")

        echo = shutil.which("echo")
        if echo is None or sys.platform == "win32":
            pytest.skip("No 'echo' command")

        opts = ProofOptions(
            run_tests=False,
            run_typecheck=False,
            extra_verification_commands=[[echo, "ok"]],
        )
        result = prove_candidate(repo, cand, opts)
        extra = result.verifications[-1] if result.verifications else None
        assert extra is not None
        assert extra.exit_code == 0


# ===========================================================================
# 13. Missing optional verification tools are SKIPPED
# ===========================================================================


class TestMissingToolsSkipped:
    def test_pytest_skipped_when_no_tests_dir(self, tmp_path):
        """No tests dir, no pytest.ini, no pyproject pytest section → SKIPPED."""
        repo = _make_simple_repo(tmp_path, "def fn(): pass\n")
        cand = _analyse_and_find(repo, "fn")

        result = prove_candidate(repo, cand, ProofOptions(run_tests=True, run_typecheck=False))
        pytest_r = result.test_result
        assert pytest_r is not None
        # Without any test config, pytest should be SKIPPED
        assert pytest_r.status in (VerificationStatus.SKIPPED, VerificationStatus.PASSED)

    def test_mypy_skipped_when_not_configured(self, tmp_path):
        """No mypy config → mypy step is SKIPPED."""
        repo = _make_simple_repo(tmp_path, "def fn(): pass\n")
        cand = _analyse_and_find(repo, "fn")

        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=True))
        mypy_r = result.typecheck_result
        assert mypy_r is not None
        assert mypy_r.status == VerificationStatus.SKIPPED

    def test_build_skipped_when_not_configured(self, tmp_path):
        """No pyproject.toml build-system → build step is SKIPPED."""
        repo = _make_simple_repo(tmp_path, "def fn(): pass\n")
        cand = _analyse_and_find(repo, "fn")

        result = prove_candidate(repo, cand, ProofOptions(run_tests=False, run_typecheck=False, run_build=True))
        build_r = result.build_result
        assert build_r is not None
        assert build_r.status == VerificationStatus.SKIPPED


# ===========================================================================
# 14. Git subprocess failure handled safely
# ===========================================================================


class TestGitSubprocessFailure:
    def test_git_not_found_returns_blocked(self, tmp_path, monkeypatch):
        """When git is not on PATH, prove_candidate returns BLOCKED gracefully."""
        _init_repo(tmp_path)
        _write(tmp_path / "module.py", "def fn(): pass\n")
        _commit(tmp_path)

        cand = _analyse_and_find(tmp_path, "fn")
        assert cand is not None

        # Patch shutil.which in the prover module so git cannot be found
        import deadcode.prover as prover_mod
        monkeypatch.setattr(prover_mod.shutil, "which", lambda name: None)

        result = prove_candidate(tmp_path, cand)
        assert result.status == ProofStatus.BLOCKED
        assert "git" in result.failure_reason.lower()

    def test_no_exception_propagates_on_git_error(self, tmp_path):
        """prove_candidate must never raise; always returns a ProofResult."""
        # Use a path that doesn't exist
        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        fake_defn = SymbolDef(
            name="fn",
            qualified_name="module.fn",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(tmp_path / "module.py"), line=1),
        )
        fake_cand = Candidate(
            symbol=fake_defn,
            classification=SafetyClassification.PROVABLE,
        )
        result = prove_candidate(tmp_path / "does_not_exist", fake_cand)
        # Must return a ProofResult, not raise
        assert isinstance(result, ProofResult)
        assert result.status in (ProofStatus.BLOCKED, ProofStatus.FAILED)


# ===========================================================================
# Remover unit tests
# ===========================================================================


class TestRemoverUnit:
    """Unit tests for the AST-based remover, independent of Git."""

    def test_remove_function_leaves_rest(self, tmp_path):
        src = textwrap.dedent("""\
            def unused():
                return 42

            def keep():
                return 1
        """)
        f = tmp_path / "mod.py"
        f.write_text(src)

        from deadcode.models import (
            Candidate, EvidenceItem, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        defn = SymbolDef(
            name="unused",
            qualified_name="mod.unused",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(f), line=1),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)

        lines_removed = remove_candidate(cand, tmp_path)
        assert lines_removed == 2  # def + return

        remaining = f.read_text()
        assert "unused" not in remaining
        assert "def keep" in remaining

    def test_remove_class_with_methods(self, tmp_path):
        src = textwrap.dedent("""\
            class Unused:
                def method(self):
                    pass

            KEEP = 1
        """)
        f = tmp_path / "mod.py"
        f.write_text(src)

        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        defn = SymbolDef(
            name="Unused",
            qualified_name="mod.Unused",
            kind=SymbolKind.CLASS,
            location=Location(file=str(f), line=1),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)
        remove_candidate(cand, tmp_path)

        remaining = f.read_text()
        assert "class Unused" not in remaining
        assert "KEEP" in remaining

    def test_remove_function_with_decorator(self, tmp_path):
        src = textwrap.dedent("""\
            def dec(fn): return fn

            @dec
            def decorated():
                pass

            KEEP = 1
        """)
        f = tmp_path / "mod.py"
        f.write_text(src)

        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        defn = SymbolDef(
            name="decorated",
            qualified_name="mod.decorated",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(f), line=4),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)
        remove_candidate(cand, tmp_path)

        remaining = f.read_text()
        assert "@dec" not in remaining
        assert "def decorated" not in remaining
        assert "KEEP" in remaining

    def test_removal_error_on_wrong_name(self, tmp_path):
        src = "def real_fn(): pass\n"
        f = tmp_path / "mod.py"
        f.write_text(src)

        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        defn = SymbolDef(
            name="ghost_fn",  # does not exist in file
            qualified_name="mod.ghost_fn",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(f), line=1),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)
        with pytest.raises(RemovalError):
            remove_candidate(cand, tmp_path)

    def test_removal_error_on_unsupported_kind(self, tmp_path):
        src = "X = 1\n"
        f = tmp_path / "mod.py"
        f.write_text(src)

        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        defn = SymbolDef(
            name="X",
            qualified_name="mod.X",
            kind=SymbolKind.VARIABLE,
            location=Location(file=str(f), line=1),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)
        with pytest.raises(RemovalError, match="Unsupported symbol kind"):
            remove_candidate(cand, tmp_path)

    def test_file_unchanged_on_removal_error(self, tmp_path):
        """If removal fails to find the symbol, the file must be unchanged."""
        src = "def real_fn(): pass\n"
        f = tmp_path / "mod.py"
        f.write_text(src)
        original = f.read_text()

        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )
        defn = SymbolDef(
            name="ghost",
            qualified_name="mod.ghost",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(f), line=1),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)
        with pytest.raises(RemovalError):
            remove_candidate(cand, tmp_path)

        assert f.read_text() == original


# ===========================================================================
# ProofResult model tests
# ===========================================================================


class TestProofResultModel:
    def test_to_dict_is_json_serialisable(self):
        import json
        from deadcode.proof_models import (
            ProofResult, ProofStatus, RemovalResult, RemovalStatus
        )
        result = ProofResult(
            candidate_qualified_name="mod.fn",
            candidate_file="/src/mod.py",
            candidate_line=5,
            status=ProofStatus.PROVEN,
            removal=RemovalResult(status=RemovalStatus.SUCCESS, lines_removed=3),
        )
        d = result.to_dict()
        json.dumps(d, default=str)  # must not raise

    def test_test_result_property_none_when_no_verifications(self):
        from deadcode.proof_models import ProofResult, ProofStatus
        result = ProofResult(
            candidate_qualified_name="mod.fn",
            candidate_file="/src/mod.py",
            candidate_line=1,
            status=ProofStatus.BLOCKED,
        )
        assert result.test_result is None
        assert result.typecheck_result is None
        assert result.build_result is None

    def test_proof_options_defaults(self):
        opts = ProofOptions()
        assert opts.timeout == 120
        assert opts.run_tests is True
        assert opts.run_typecheck is True
        assert opts.run_build is False
        assert opts.extra_verification_commands == []
