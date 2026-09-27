"""
tests/test_cli.py – CLI integration tests using typer.testing.CliRunner.

Tests create isolated temporary directories (not Git repos unless noted).
They do NOT depend on this project's own repository.

Coverage:
 1. deadcode --help
 2. deadcode scan
 3. Scan candidate output
 4. Deterministic candidate IDs
 5. deadcode show
 6. deadcode prove valid candidate (mocked proof engine)
 7. prove REVIEW candidate → BLOCKED
 8. prove unknown candidate ID
 9. apply unproven candidate → BLOCKED
10. apply proven candidate requires confirmation
11. apply declined
12. apply confirmed (--yes)
13. CLI handles invalid repository path
14. CLI preserves useful proof failure output
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deadcode.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_git_repo(path: Path) -> None:
    _git(["git", "init", "-b", "main"], path)
    _git(["git", "config", "user.email", "test@deadcode.test"], path)
    _git(["git", "config", "user.name", "DeadCode Test"], path)


def _commit(path: Path, msg: str = "init") -> None:
    _git(["git", "add", "."], path)
    _git(["git", "commit", "-m", msg, "--allow-empty"], path)


def _make_simple_project(tmp_path: Path) -> Path:
    """Create a minimal Python project with one unused function."""
    _write(tmp_path / "utils.py", """\
        def unused_fn():
            return 42

        def used_fn():
            return 1

        result = used_fn()
    """)
    return tmp_path


def _scan_and_get_state(tmp_path: Path) -> dict:
    """Run scan on tmp_path and return the parsed state dict."""
    result = runner.invoke(app, ["scan", str(tmp_path)])
    assert result.exit_code == 0, result.output
    state_file = tmp_path / ".deadcode" / "state.json"
    assert state_file.exists(), "State file not created"
    return json.loads(state_file.read_text())


# ===========================================================================
# 1. deadcode --help
# ===========================================================================


class TestHelp:
    def test_help_exits_zero(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0

    def test_help_lists_commands(self):
        result = runner.invoke(app, ["--help"])
        assert "scan" in result.output
        assert "show" in result.output
        assert "prove" in result.output
        assert "apply" in result.output

    def test_version_flag(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "deadcode" in result.output.lower()

    def test_scan_help(self):
        result = runner.invoke(app, ["scan", "--help"])
        assert result.exit_code == 0
        assert "PATH" in result.output or "path" in result.output.lower()

    def test_prove_help(self):
        result = runner.invoke(app, ["prove", "--help"])
        assert result.exit_code == 0


# ===========================================================================
# 2. deadcode scan
# ===========================================================================


class TestScan:
    def test_scan_exits_zero(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert result.exit_code == 0

    def test_scan_shows_repo_path(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert str(tmp_path) in result.output

    def test_scan_shows_files_scanned(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert "Files scanned" in result.output or "files" in result.output.lower()

    def test_scan_creates_state_file(self, tmp_path):
        _make_simple_project(tmp_path)
        runner.invoke(app, ["scan", str(tmp_path)])
        state_file = tmp_path / ".deadcode" / "state.json"
        assert state_file.exists()

    def test_scan_state_is_valid_json(self, tmp_path):
        _make_simple_project(tmp_path)
        runner.invoke(app, ["scan", str(tmp_path)])
        state_file = tmp_path / ".deadcode" / "state.json"
        state = json.loads(state_file.read_text())
        assert "candidates" in state
        assert "version" in state

    def test_scan_invalid_path_exits_nonzero(self):
        result = runner.invoke(app, ["scan", "/nonexistent/path/xyz"])
        assert result.exit_code != 0


# ===========================================================================
# 3. Scan candidate output
# ===========================================================================


class TestScanOutput:
    def test_provable_candidates_shown(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert "PROVABLE" in result.output

    def test_candidate_shows_function_name(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert "unused_fn" in result.output

    def test_candidate_shows_file_and_line(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        # Should contain utils.py with a line number
        assert "utils.py" in result.output

    def test_review_section_present(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        assert "REVIEW" in result.output

    def test_active_not_shown_by_default(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path)])
        # ACTIVE section should not appear at all without --active flag
        assert "ACTIVE CANDIDATES" not in result.output

    def test_active_shown_with_flag(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path), "--active"])
        assert "ACTIVE CANDIDATES" in result.output
        assert "used_fn" in result.output

    def test_only_filter_provable(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path), "--only", "PROVABLE"])
        assert result.exit_code == 0
        assert "PROVABLE" in result.output

    def test_only_filter_review(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path), "--only", "REVIEW"])
        assert result.exit_code == 0
        assert "REVIEW" in result.output

    def test_only_filter_active(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path), "--only", "ACTIVE"])
        assert result.exit_code == 0
        assert "ACTIVE" in result.output

    def test_only_filter_invalid_exits_nonzero(self, tmp_path):
        _make_simple_project(tmp_path)
        result = runner.invoke(app, ["scan", str(tmp_path), "--only", "GARBAGE"])
        assert result.exit_code != 0


# ===========================================================================
# 4. Deterministic candidate IDs
# ===========================================================================


class TestDeterministicIDs:
    def test_ids_are_deterministic(self, tmp_path):
        """Running scan twice produces identical IDs."""
        _make_simple_project(tmp_path)
        state1 = _scan_and_get_state(tmp_path)
        state2 = _scan_and_get_state(tmp_path)
        assert set(state1["candidates"].keys()) == set(state2["candidates"].keys())

    def test_ids_start_at_dc_001(self, tmp_path):
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        ids = list(state["candidates"].keys())
        assert "DC-001" in ids

    def test_ids_are_dc_format(self, tmp_path):
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        for dc_id in state["candidates"].keys():
            assert dc_id.startswith("DC-")
            assert dc_id[3:].isdigit()

    def test_same_candidate_same_id_across_runs(self, tmp_path):
        _make_simple_project(tmp_path)
        state1 = _scan_and_get_state(tmp_path)
        state2 = _scan_and_get_state(tmp_path)
        # Check that for each qualified name the ID is the same
        qname_to_id1 = {
            v["qualified_name"]: k for k, v in state1["candidates"].items()
        }
        qname_to_id2 = {
            v["qualified_name"]: k for k, v in state2["candidates"].items()
        }
        assert qname_to_id1 == qname_to_id2


# ===========================================================================
# 5. deadcode show
# ===========================================================================


class TestShow:
    def _get_first_id(self, tmp_path: Path) -> str:
        state = _scan_and_get_state(tmp_path)
        return next(iter(state["candidates"]))

    def test_show_exits_zero(self, tmp_path):
        _make_simple_project(tmp_path)
        dc_id = self._get_first_id(tmp_path)
        result = runner.invoke(app, ["show", dc_id, "--path", str(tmp_path)])
        assert result.exit_code == 0

    def test_show_displays_symbol_name(self, tmp_path):
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id, raw = next(iter(state["candidates"].items()))
        result = runner.invoke(app, ["show", dc_id, "--path", str(tmp_path)])
        assert raw["name"] in result.output

    def test_show_displays_classification(self, tmp_path):
        _make_simple_project(tmp_path)
        dc_id = self._get_first_id(tmp_path)
        result = runner.invoke(app, ["show", dc_id, "--path", str(tmp_path)])
        assert any(cls in result.output for cls in ("PROVABLE", "REVIEW", "ACTIVE"))

    def test_show_displays_file_and_line(self, tmp_path):
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(iter(state["candidates"]))
        result = runner.invoke(app, ["show", dc_id, "--path", str(tmp_path)])
        assert "utils.py" in result.output

    def test_show_unknown_id_exits_nonzero(self, tmp_path):
        _make_simple_project(tmp_path)
        runner.invoke(app, ["scan", str(tmp_path)])  # create state
        result = runner.invoke(app, ["show", "DC-999", "--path", str(tmp_path)])
        assert result.exit_code != 0

    def test_show_no_state_file_exits_nonzero(self, tmp_path):
        _make_simple_project(tmp_path)
        # Do not run scan first
        result = runner.invoke(app, ["show", "DC-001", "--path", str(tmp_path)])
        assert result.exit_code != 0


# ===========================================================================
# 6. deadcode prove valid candidate (mocked)
# ===========================================================================


class TestProveValidCandidate:
    """
    prove tests use monkeypatching to avoid real Git operations.
    We patch prove_candidate in cli.py to return a pre-built ProofResult.
    """

    def _make_provable_state(self, tmp_path: Path) -> str:
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        # Find the first PROVABLE candidate
        for dc_id, raw in state["candidates"].items():
            if raw["classification"] == "PROVABLE":
                return dc_id
        pytest.skip("No PROVABLE candidate found in fixture")

    def test_prove_calls_engine(self, tmp_path, monkeypatch):
        from deadcode.proof_models import (
            ProofResult, ProofStatus, ReanalysisResult,
            RemovalResult, RemovalStatus,
        )
        import deadcode.cli as cli_mod

        dc_id = self._make_provable_state(tmp_path)
        mock_result = ProofResult(
            candidate_qualified_name="utils.unused_fn",
            candidate_file=str(tmp_path / "utils.py"),
            candidate_line=1,
            status=ProofStatus.PROVEN,
            removal=RemovalResult(status=RemovalStatus.SUCCESS, lines_removed=2),
            reanalysis=ReanalysisResult(candidate_absent=True, total_candidates_after=0),
            worktree_id="deadcode_wt_test",
        )
        monkeypatch.setattr(cli_mod, "prove_candidate", lambda *a, **kw: mock_result)

        result = runner.invoke(app, ["prove", dc_id, "--path", str(tmp_path)])
        assert "PROVEN" in result.output

    def test_prove_records_result_in_state(self, tmp_path, monkeypatch):
        from deadcode.proof_models import (
            ProofResult, ProofStatus, ReanalysisResult,
            RemovalResult, RemovalStatus,
        )
        import deadcode.cli as cli_mod

        dc_id = self._make_provable_state(tmp_path)
        mock_result = ProofResult(
            candidate_qualified_name="utils.unused_fn",
            candidate_file=str(tmp_path / "utils.py"),
            candidate_line=1,
            status=ProofStatus.PROVEN,
            removal=RemovalResult(status=RemovalStatus.SUCCESS, lines_removed=2),
            reanalysis=ReanalysisResult(candidate_absent=True),
            worktree_id="wt_test",
        )
        monkeypatch.setattr(cli_mod, "prove_candidate", lambda *a, **kw: mock_result)

        runner.invoke(app, ["prove", dc_id, "--path", str(tmp_path)])

        from deadcode.state import is_proven
        assert is_proven(tmp_path, dc_id)


# ===========================================================================
# 7. prove REVIEW candidate → BLOCKED
# ===========================================================================


class TestProveReviewBlocked:
    def test_review_candidate_returns_blocked(self, tmp_path):
        """A REVIEW candidate must be refused by the prove command."""
        _write(tmp_path / "module.py", """\
            def my_dec(fn): return fn

            @my_dec
            def decorated_fn(): pass
        """)
        # Scan to populate state
        runner.invoke(app, ["scan", str(tmp_path)])

        # Find the REVIEW candidate
        state_file = tmp_path / ".deadcode" / "state.json"
        state = json.loads(state_file.read_text())
        review_id = next(
            (dc_id for dc_id, raw in state["candidates"].items()
             if raw["classification"] == "REVIEW" and raw["name"] == "decorated_fn"),
            None,
        )
        if review_id is None:
            pytest.skip("decorated_fn not classified REVIEW")

        result = runner.invoke(app, ["prove", review_id, "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "BLOCKED" in result.output or "REVIEW" in result.output


# ===========================================================================
# 8. prove unknown candidate ID
# ===========================================================================


class TestProveUnknownId:
    def test_unknown_id_exits_nonzero(self, tmp_path):
        _make_simple_project(tmp_path)
        runner.invoke(app, ["scan", str(tmp_path)])
        result = runner.invoke(app, ["prove", "DC-999", "--path", str(tmp_path)])
        assert result.exit_code != 0

    def test_unknown_id_without_state_exits_nonzero(self, tmp_path):
        result = runner.invoke(app, ["prove", "DC-001", "--path", str(tmp_path)])
        assert result.exit_code != 0


# ===========================================================================
# 9. apply unproven candidate → BLOCKED
# ===========================================================================


class TestApplyUnprovenBlocked:
    def test_apply_without_proof_is_blocked(self, tmp_path):
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(iter(state["candidates"]))
        result = runner.invoke(app, ["apply", dc_id, "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "BLOCKED" in result.output or "proven" in result.output.lower()

    def test_apply_without_scan_exits_nonzero(self, tmp_path):
        result = runner.invoke(app, ["apply", "DC-001", "--path", str(tmp_path)])
        assert result.exit_code != 0


# ===========================================================================
# 10. apply proven candidate requires confirmation
# ===========================================================================


class TestApplyRequiresConfirmation:
    def _setup_proven(self, tmp_path: Path) -> str:
        """Scan + inject a fake proof record; return the dc_id."""
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(
            (k for k, v in state["candidates"].items() if v["classification"] == "PROVABLE"),
            None,
        )
        if dc_id is None:
            pytest.skip("No PROVABLE candidate")
        from deadcode.state import record_proof
        record_proof(tmp_path, dc_id, {"status": "PROVEN"})
        return dc_id

    def test_apply_asks_for_confirmation(self, tmp_path):
        dc_id = self._setup_proven(tmp_path)
        # Provide 'n' as input — should cancel
        result = runner.invoke(
            app, ["apply", dc_id, "--path", str(tmp_path)], input="n\n"
        )
        # Should show the confirmation prompt
        assert "y/N" in result.output or "confirm" in result.output.lower() or "Apply" in result.output


# ===========================================================================
# 11. apply declined
# ===========================================================================


class TestApplyDeclined:
    def _setup_proven(self, tmp_path: Path) -> str:
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(
            (k for k, v in state["candidates"].items()
             if v["classification"] == "PROVABLE"
             and v["kind"] in ("function", "async_function", "class")),
            None,
        )
        if dc_id is None:
            pytest.skip("No PROVABLE function/class candidate")
        from deadcode.state import record_proof
        record_proof(tmp_path, dc_id, {"status": "PROVEN"})
        return dc_id

    def test_apply_declined_preserves_file(self, tmp_path):
        dc_id = self._setup_proven(tmp_path)
        original = (tmp_path / "utils.py").read_text()
        runner.invoke(
            app, ["apply", dc_id, "--path", str(tmp_path)], input="n\n"
        )
        assert (tmp_path / "utils.py").read_text() == original

    def test_apply_declined_exits_zero(self, tmp_path):
        dc_id = self._setup_proven(tmp_path)
        result = runner.invoke(
            app, ["apply", dc_id, "--path", str(tmp_path)], input="n\n"
        )
        # Declining is not an error
        assert result.exit_code == 0


# ===========================================================================
# 12. apply confirmed (--yes)
# ===========================================================================


class TestApplyConfirmed:
    def _setup_proven(self, tmp_path: Path) -> str:
        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(
            (k for k, v in state["candidates"].items()
             if v["classification"] == "PROVABLE"
             and v["kind"] in ("function", "async_function", "class")),
            None,
        )
        if dc_id is None:
            pytest.skip("No PROVABLE function/class candidate")
        from deadcode.state import record_proof
        record_proof(tmp_path, dc_id, {"status": "PROVEN"})
        return dc_id

    def test_apply_yes_removes_function(self, tmp_path):
        dc_id = self._setup_proven(tmp_path)
        original = (tmp_path / "utils.py").read_text()
        result = runner.invoke(
            app, ["apply", dc_id, "--path", str(tmp_path), "--yes"]
        )
        assert result.exit_code == 0
        new_content = (tmp_path / "utils.py").read_text()
        assert new_content != original
        # The candidate (unused_fn) should no longer appear
        assert "unused_fn" not in new_content

    def test_apply_yes_shows_applied_message(self, tmp_path):
        dc_id = self._setup_proven(tmp_path)
        result = runner.invoke(
            app, ["apply", dc_id, "--path", str(tmp_path), "--yes"]
        )
        assert "APPLIED" in result.output or "removed" in result.output.lower()

    def test_apply_yes_removes_class_method(self, tmp_path):
        _make_simple_project(tmp_path)
        # Add a class with an unused method
        (tmp_path / "resource.py").write_text(
            "class FizzbarResource:\n"
            "    def get(self):\n"
            "        return 1\n"
            "    def delete(self):\n"
            "        return 2\n"
        )
        scan_res = runner.invoke(app, ["scan", str(tmp_path)])
        assert scan_res.exit_code == 0

        state = _scan_and_get_state(tmp_path)
        dc_id = next(
            k for k, v in state["candidates"].items()
            if v["name"] == "delete" and v["classification"] == "PROVABLE"
        )

        from deadcode.state import record_proof
        record_proof(
            tmp_path,
            dc_id,
            {
                "status": "PROVEN",
                "candidate_qualified_name": state["candidates"][dc_id]["qualified_name"],
            },
        )

        apply_res = runner.invoke(app, ["apply", dc_id, "--path", str(tmp_path), "--yes"])
        assert apply_res.exit_code == 0
        assert "Removed" in apply_res.output

        new_content = (tmp_path / "resource.py").read_text()
        assert "def delete" not in new_content
        assert "def get" in new_content
        assert "class FizzbarResource:" in new_content


# ===========================================================================
# 13. CLI handles invalid repository path
# ===========================================================================


class TestInvalidPath:
    def test_scan_nonexistent_path(self):
        result = runner.invoke(app, ["scan", "/does/not/exist/xyz"])
        assert result.exit_code != 0

    def test_show_nonexistent_path(self):
        result = runner.invoke(app, ["show", "DC-001", "--path", "/does/not/exist"])
        assert result.exit_code != 0

    def test_prove_nonexistent_path(self):
        result = runner.invoke(app, ["prove", "DC-001", "--path", "/does/not/exist"])
        assert result.exit_code != 0

    def test_apply_nonexistent_path(self):
        result = runner.invoke(app, ["apply", "DC-001", "--path", "/does/not/exist"])
        assert result.exit_code != 0


# ===========================================================================
# 14. CLI preserves useful proof failure output
# ===========================================================================


class TestProofFailureOutput:
    def test_proof_failure_shows_reason(self, tmp_path, monkeypatch):
        from deadcode.proof_models import (
            ProofResult, ProofStatus,
            RemovalResult, RemovalStatus,
            VerificationResult, VerificationStatus,
        )
        import deadcode.cli as cli_mod

        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(
            (k for k, v in state["candidates"].items() if v["classification"] == "PROVABLE"),
            None,
        )
        if dc_id is None:
            pytest.skip("No PROVABLE candidate")

        failed_result = ProofResult(
            candidate_qualified_name="utils.unused_fn",
            candidate_file=str(tmp_path / "utils.py"),
            candidate_line=1,
            status=ProofStatus.FAILED,
            failure_reason="Tests failed: 1 assertion error",
            removal=RemovalResult(status=RemovalStatus.SUCCESS, lines_removed=2),
            verifications=[
                VerificationResult(
                    name="pytest",
                    status=VerificationStatus.FAILED,
                    exit_code=1,
                    reason="1 assertion error",
                    stdout="FAILED test_utils.py::test_fn",
                )
            ],
            worktree_id="wt_fail",
        )
        monkeypatch.setattr(cli_mod, "prove_candidate", lambda *a, **kw: failed_result)

        result = runner.invoke(app, ["prove", dc_id, "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "FAILED" in result.output
        assert "Tests failed" in result.output or "assertion error" in result.output or "PROOF FAILED" in result.output

    def test_blocked_shows_reason(self, tmp_path, monkeypatch):
        from deadcode.proof_models import (
            ProofResult, ProofStatus,
        )
        import deadcode.cli as cli_mod

        _make_simple_project(tmp_path)
        state = _scan_and_get_state(tmp_path)
        dc_id = next(
            (k for k, v in state["candidates"].items() if v["classification"] == "PROVABLE"),
            None,
        )
        if dc_id is None:
            pytest.skip("No PROVABLE candidate")

        blocked_result = ProofResult(
            candidate_qualified_name="utils.unused_fn",
            candidate_file=str(tmp_path / "utils.py"),
            candidate_line=1,
            status=ProofStatus.BLOCKED,
            failure_reason="git not found on PATH",
        )
        monkeypatch.setattr(cli_mod, "prove_candidate", lambda *a, **kw: blocked_result)

        result = runner.invoke(app, ["prove", dc_id, "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "BLOCKED" in result.output
        assert "git" in result.output.lower()


# ===========================================================================
# State module unit tests
# ===========================================================================


class TestStateModule:
    def test_assign_ids_deterministic(self):
        from deadcode.state import assign_ids
        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )

        def make_cand(name: str, cls: SafetyClassification) -> Candidate:
            return Candidate(
                symbol=SymbolDef(
                    name=name,
                    qualified_name=f"mod.{name}",
                    kind=SymbolKind.FUNCTION,
                    location=Location(file="mod.py", line=1),
                ),
                classification=cls,
            )

        cands = [
            make_cand("b", SafetyClassification.PROVABLE),
            make_cand("a", SafetyClassification.PROVABLE),
        ]
        result1 = assign_ids(cands)
        result2 = assign_ids(cands)
        assert list(result1.keys()) == list(result2.keys())

    def test_provable_gets_lower_ids(self):
        from deadcode.state import assign_ids
        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )

        def make_cand(name: str, cls: SafetyClassification) -> Candidate:
            return Candidate(
                symbol=SymbolDef(
                    name=name,
                    qualified_name=f"mod.{name}",
                    kind=SymbolKind.FUNCTION,
                    location=Location(file="mod.py", line=1),
                ),
                classification=cls,
            )

        cands = [
            make_cand("review_fn", SafetyClassification.REVIEW),
            make_cand("provable_fn", SafetyClassification.PROVABLE),
        ]
        id_map = assign_ids(cands)
        ids = list(id_map.keys())
        # DC-001 should be the PROVABLE one
        assert id_map["DC-001"].symbol.name == "provable_fn"

    def test_save_and_load_round_trip(self, tmp_path):
        from deadcode.state import assign_ids, save_state, load_candidate
        from deadcode.models import (
            Candidate, Location, SafetyClassification, SymbolDef, SymbolKind
        )

        defn = SymbolDef(
            name="fn",
            qualified_name="mod.fn",
            kind=SymbolKind.FUNCTION,
            location=Location(file=str(tmp_path / "mod.py"), line=5),
        )
        cand = Candidate(symbol=defn, classification=SafetyClassification.PROVABLE)
        id_map = assign_ids([cand])
        save_state(tmp_path, id_map)

        loaded = load_candidate(tmp_path, "DC-001")
        assert loaded is not None
        assert loaded.symbol.name == "fn"
        assert loaded.classification == SafetyClassification.PROVABLE