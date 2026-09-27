"""
prover.py – isolated Git-worktree proof engine.

Public API
----------
    from deadcode.prover import prove_candidate, ProofOptions

    result = prove_candidate(repo_path, candidate)
    result = prove_candidate(repo_path, candidate, options=ProofOptions(timeout=120))

Proof lifecycle
---------------
1. Pre-flight checks (BLOCKED-exit conditions):
   - Candidate classification must be PROVABLE.
   - Repository must be a Git repo with a clean-enough state.
   - Git must be available on PATH.
   - Symbol kind must be supported (function / class).

2. Create an isolated Git worktree:
   - ``git worktree add --detach <tmpdir> HEAD``
   - tmpdir lives in the system temp directory (never inside the repo).

3. Remove the candidate definition (AST-based, via ``remover.py``).

4. Run verification commands inside the worktree (pytest, mypy):
   - Each command is probed for availability before running.
   - SKIPPED if not configured; FAILED if non-zero exit.
   - stdout/stderr are captured and truncated.

5. Re-run DeadCode analysis on the worktree:
   - Confirm the target qualified name is absent.

6. Cleanup the worktree (``git worktree remove --force <tmpdir>``):
   - Runs in a ``finally`` block; errors are suppressed.

7. Return a ``ProofResult``.

Safety guarantees
-----------------
* The user's working tree is never modified.
* Only the isolated worktree (in system temp) is modified.
* Cleanup runs unconditionally in ``finally``.
* Shell injection is impossible: all subprocess calls use list-form args
  and ``shell=False``.
* ``git worktree add --detach`` creates a new detached HEAD — the user's
  branch pointer is never touched.
* No commits, no pushes, no resets on the user's checkout.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from deadcode.analyzer import analyze_repo
from deadcode.models import Candidate, SafetyClassification, SymbolKind
from deadcode.proof_models import (
    ProofResult,
    ProofStatus,
    ReanalysisResult,
    RemovalResult,
    RemovalStatus,
    VerificationResult,
    VerificationStatus,
)
from deadcode.remover import RemovalError, remove_candidate


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class ProofOptions:
    """
    Configurable options for a single proof attempt.

    Attributes
    ----------
    timeout:
        Maximum wall-clock seconds for each individual verification command.
        Default is 120 s.
    max_output_bytes:
        Maximum bytes captured from stdout/stderr per command before
        truncation.  Default is 65 536 (64 KiB).
    run_tests:
        Whether to attempt running pytest if detected.  Default True.
    run_typecheck:
        Whether to attempt running mypy if detected.  Default True.
    run_build:
        Whether to attempt a build check if detected.  Default False
        (build checks are slow and often not necessary for function removal).
    extra_verification_commands:
        Additional commands to run after standard checks.  Each entry is a
        list of strings (argv).  These are always run if the prior steps pass.
    """

    timeout: int = 120
    max_output_bytes: int = 65_536
    run_tests: bool = True
    run_typecheck: bool = True
    run_build: bool = False
    extra_verification_commands: list[list[str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Supported symbol kinds
# ---------------------------------------------------------------------------

_SUPPORTED_KINDS: frozenset[SymbolKind] = frozenset(
    {
        SymbolKind.FUNCTION,
        SymbolKind.ASYNC_FUNCTION,
        SymbolKind.CLASS,
        SymbolKind.MODULE,
    }
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def prove_candidate(
    repository_path: str | Path,
    candidate: Candidate,
    options: ProofOptions | None = None,
) -> ProofResult:
    """
    Attempt to prove that *candidate* can be safely removed.

    Parameters
    ----------
    repository_path:
        Absolute path to the root of the Git repository to analyse.
    candidate:
        The :class:`~deadcode.models.Candidate` to prove.  Must have been
        produced by ``analyze_repo(repository_path)``.
    options:
        Optional :class:`ProofOptions`.  Uses defaults when omitted.

    Returns
    -------
    ProofResult
        Structured result.  The caller's working tree is guaranteed to be
        unmodified regardless of the result status.
    """
    opts = options or ProofOptions()
    repo_root = Path(repository_path).resolve()
    start_time = time.monotonic()

    # Candidate identity (captured early so we can always fill the result)
    qname = candidate.qualified_name
    cfile = candidate.file
    cline = candidate.line

    def _elapsed() -> float:
        return time.monotonic() - start_time

    def _blocked(reason: str) -> ProofResult:
        return ProofResult(
            candidate_qualified_name=qname,
            candidate_file=cfile,
            candidate_line=cline,
            status=ProofStatus.BLOCKED,
            failure_reason=reason,
            duration_seconds=_elapsed(),
        )

    def _failed(
        reason: str,
        removal: RemovalResult | None = None,
        verifications: list[VerificationResult] | None = None,
        reanalysis: ReanalysisResult | None = None,
        worktree_id: str = "",
    ) -> ProofResult:
        return ProofResult(
            candidate_qualified_name=qname,
            candidate_file=cfile,
            candidate_line=cline,
            status=ProofStatus.FAILED,
            failure_reason=reason,
            removal=removal or RemovalResult(status=RemovalStatus.NOT_ATTEMPTED),
            verifications=verifications or [],
            reanalysis=reanalysis or ReanalysisResult(),
            worktree_id=worktree_id,
            duration_seconds=_elapsed(),
        )

    # ----------------------------------------------------------------
    # Pre-flight: classification check
    # ----------------------------------------------------------------
    if candidate.classification != SafetyClassification.PROVABLE:
        return _blocked(
            f"Candidate '{qname}' is classified "
            f"{candidate.classification.value}, not PROVABLE.  "
            "Only PROVABLE candidates can be proven."
        )

    # ----------------------------------------------------------------
    # Pre-flight: supported symbol kind
    # ----------------------------------------------------------------
    if candidate.symbol.kind not in _SUPPORTED_KINDS:
        return _blocked(
            f"Symbol kind '{candidate.symbol.kind.value}' is not yet supported "
            "for removal.  Only module-level functions, classes, and modules are supported."
        )

    # ----------------------------------------------------------------
    # Pre-flight: Git availability
    # ----------------------------------------------------------------
    git_exe = shutil.which("git")
    if git_exe is None:
        return _blocked("'git' executable not found on PATH.")

    # ----------------------------------------------------------------
    # Pre-flight: repository is a Git repo
    # ----------------------------------------------------------------
    is_git, git_reason = _check_git_repo(repo_root, git_exe)
    if not is_git:
        return _blocked(f"Not a valid Git repository: {git_reason}")

    # ----------------------------------------------------------------
    # Main proof flow with guaranteed worktree cleanup
    # ----------------------------------------------------------------
    worktree_path: Path | None = None
    worktree_id = ""

    try:
        # ---- Create worktree ----
        worktree_path, wt_error = _create_worktree(repo_root, git_exe)
        if worktree_path is None:
            return _failed(f"Failed to create Git worktree: {wt_error}")

        worktree_id = worktree_path.name

        # ---- Remove candidate ----
        removal = _do_removal(candidate, worktree_path)
        if removal.status != RemovalStatus.SUCCESS:
            return _failed(
                reason=f"Candidate removal failed: {removal.reason}",
                removal=removal,
                worktree_id=worktree_id,
            )

        # ---- Verification commands ----
        verifications = _run_verifications(worktree_path, opts)

        # Check whether any *required* (non-skipped) verification failed
        failed_verifications = [
            v for v in verifications if v.status == VerificationStatus.FAILED
        ]
        if failed_verifications:
            names = ", ".join(v.name for v in failed_verifications)
            return _failed(
                reason=f"Verification failed: {names}",
                removal=removal,
                verifications=verifications,
                worktree_id=worktree_id,
            )

        # ---- Re-analysis ----
        reanalysis = _do_reanalysis(worktree_path, qname)
        if not reanalysis.candidate_absent:
            reason = reanalysis.error or (
                f"Candidate '{qname}' still appears after removal"
            )
            return _failed(
                reason=reason,
                removal=removal,
                verifications=verifications,
                reanalysis=reanalysis,
                worktree_id=worktree_id,
            )

        # ---- PROVEN ----
        return ProofResult(
            candidate_qualified_name=qname,
            candidate_file=cfile,
            candidate_line=cline,
            status=ProofStatus.PROVEN,
            removal=removal,
            verifications=verifications,
            reanalysis=reanalysis,
            worktree_id=worktree_id,
            duration_seconds=_elapsed(),
        )

    except Exception as exc:  # noqa: BLE001
        return _failed(
            reason=f"Unexpected error during proof: {type(exc).__name__}: {exc}",
            worktree_id=worktree_id,
        )

    finally:
        if worktree_path is not None:
            _remove_worktree(worktree_path, repo_root, git_exe)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _check_git_repo(repo_root: Path, git_exe: str) -> tuple[bool, str]:
    """
    Return (True, "") if *repo_root* is inside a Git repo, else (False, reason).
    """
    result = _git(
        [git_exe, "rev-parse", "--git-dir"],
        cwd=repo_root,
        timeout=10,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "git rev-parse failed"
    return True, ""


def _create_worktree(
    repo_root: Path, git_exe: str
) -> tuple[Path | None, str]:
    """
    Create an isolated Git worktree at a fresh temporary directory.

    Uses ``git worktree add --detach <path> HEAD`` so the user's branch
    pointer is never modified.

    Returns (worktree_path, "") on success or (None, error_message) on failure.
    """
    # Create a temp directory *outside* the repo to hold the worktree.
    # We create it ourselves so we control the location and name.
    try:
        tmp_dir = tempfile.mkdtemp(prefix="deadcode_wt_")
    except OSError as exc:
        return None, f"Cannot create temp directory: {exc}"

    wt_path = Path(tmp_dir)

    # git worktree add --detach <path> HEAD
    result = _git(
        [git_exe, "worktree", "add", "--detach", str(wt_path), "HEAD"],
        cwd=repo_root,
        timeout=30,
    )

    if result.returncode != 0:
        # Clean up the temp directory if worktree creation failed
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        err = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return None, f"git worktree add failed: {err}"

    return wt_path, ""


def _remove_worktree(
    worktree_path: Path, repo_root: Path, git_exe: str
) -> None:
    """
    Remove the isolated worktree.  Errors are suppressed — this runs in
    ``finally`` and must never propagate an exception.
    """
    try:
        _git(
            [git_exe, "worktree", "remove", "--force", str(worktree_path)],
            cwd=repo_root,
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass

    # Belt-and-suspenders: remove the directory if git didn't
    try:
        if worktree_path.exists():
            shutil.rmtree(str(worktree_path), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _git(
    args: list[str],
    cwd: Path,
    timeout: int = 30,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a Git command safely (no shell=True).

    Parameters
    ----------
    args:
        Full argument list including the git executable as the first element.
    cwd:
        Working directory for the subprocess.
    timeout:
        Seconds before the process is killed.
    env:
        Optional environment dict; defaults to inheriting the current env.

    Returns
    -------
    subprocess.CompletedProcess
        Always returns a result object (never raises CalledProcessError).
    """
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args,
            returncode=-1,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=-1,
            stdout="",
            stderr=f"Executable not found: {exc}",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            args=args,
            returncode=-1,
            stdout="",
            stderr=f"OS error: {exc}",
        )


# ---------------------------------------------------------------------------
# Candidate removal
# ---------------------------------------------------------------------------


def _do_removal(candidate: Candidate, worktree_path: Path) -> RemovalResult:
    """Remove the candidate from the worktree. Returns a RemovalResult."""
    try:
        lines_removed = remove_candidate(candidate, worktree_path)
        return RemovalResult(
            status=RemovalStatus.SUCCESS,
            file_path=candidate.file,
            lines_removed=lines_removed,
        )
    except RemovalError as exc:
        return RemovalResult(
            status=RemovalStatus.FAILED,
            file_path=candidate.file,
            reason=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return RemovalResult(
            status=RemovalStatus.FAILED,
            file_path=candidate.file,
            reason=f"Unexpected error: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _run_verifications(
    worktree_path: Path, opts: ProofOptions
) -> list[VerificationResult]:
    """
    Detect and run available verification commands in *worktree_path*.

    The order is: pytest → mypy → build → extra.
    """
    results: list[VerificationResult] = []

    if opts.run_tests:
        results.append(_run_pytest(worktree_path, opts))

    if opts.run_typecheck:
        results.append(_run_mypy(worktree_path, opts))

    if opts.run_build:
        results.append(_run_build(worktree_path, opts))

    for cmd in opts.extra_verification_commands:
        results.append(_run_command("extra", cmd, worktree_path, opts))

    return results


def _detect_pytest(worktree_path: Path) -> list[str] | None:
    """
    Return the command to run pytest if it appears to be configured,
    or ``None`` if not detected.

    Heuristics (in priority order):
    1. ``pytest.ini`` present
    2. ``setup.cfg`` with ``[tool:pytest]`` section
    3. ``pyproject.toml`` with ``[tool.pytest.ini_options]`` section
    4. ``tests/`` or ``test/`` directory present
    5. Any ``test_*.py`` or ``*_test.py`` file at root level

    Returns the *command* list (e.g. ``["python", "-m", "pytest"]``).
    ``python`` here refers to the interpreter that is on PATH in the
    worktree context.
    """
    wt = worktree_path

    # Check for standard pytest config markers
    if (wt / "pytest.ini").exists():
        return _pytest_cmd(wt)
    if (wt / "setup.cfg").exists():
        try:
            content = (wt / "setup.cfg").read_text(encoding="utf-8", errors="replace")
            if "[tool:pytest]" in content:
                return _pytest_cmd(wt)
        except OSError:
            pass
    if (wt / "pyproject.toml").exists():
        try:
            content = (wt / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest" in content:
                return _pytest_cmd(wt)
        except OSError:
            pass
    if (wt / "tests").is_dir() or (wt / "test").is_dir():
        return _pytest_cmd(wt)

    # Look for any test file in the repo root
    if any(wt.glob("test_*.py")) or any(wt.glob("*_test.py")):
        return _pytest_cmd(wt)

    return None


def _pytest_cmd(worktree_path: Path) -> list[str]:
    """
    Return the best pytest command for the worktree.

    Prefer ``pytest`` executable in the worktree's .venv if present;
    otherwise fall back to ``python -m pytest`` using the system Python.
    """
    # Check for a local venv
    for venv_dir in (".venv", "venv", "env"):
        for bin_name in ("bin/pytest", "Scripts/pytest.exe"):
            candidate_exe = worktree_path / venv_dir / bin_name
            if candidate_exe.exists():
                return [str(candidate_exe)]

    # Fall back to system python -m pytest
    python = shutil.which("python3") or shutil.which("python") or "python3"
    return [python, "-m", "pytest"]


def _detect_mypy(worktree_path: Path) -> list[str] | None:
    """
    Return the mypy command if mypy appears to be configured, else None.
    """
    wt = worktree_path

    has_config = False
    if (wt / "mypy.ini").exists() or (wt / ".mypy.ini").exists():
        has_config = True
    if not has_config and (wt / "setup.cfg").exists():
        try:
            content = (wt / "setup.cfg").read_text(encoding="utf-8", errors="replace")
            if "[mypy]" in content:
                has_config = True
        except OSError:
            pass
    if not has_config and (wt / "pyproject.toml").exists():
        try:
            content = (wt / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
            if "[tool.mypy]" in content:
                has_config = True
        except OSError:
            pass

    if not has_config:
        return None

    # Check if mypy is actually available
    mypy_exe = shutil.which("mypy")
    if mypy_exe is None:
        # Configured but not installed
        return None

    return [mypy_exe, "."]


def _detect_build(worktree_path: Path) -> list[str] | None:
    """
    Return the build command if the project is configured for packaging.
    Only returns a command when both pyproject.toml exists and declares a
    build-system.
    """
    pyproject = worktree_path / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        content = pyproject.read_text(encoding="utf-8", errors="replace")
        if "[build-system]" not in content:
            return None
    except OSError:
        return None

    python = shutil.which("python3") or shutil.which("python") or "python3"
    return [python, "-m", "build", "--no-isolation", "--wheel", "."]


def _run_pytest(worktree_path: Path, opts: ProofOptions) -> VerificationResult:
    cmd = _detect_pytest(worktree_path)
    if cmd is None:
        return VerificationResult(
            name="pytest",
            status=VerificationStatus.SKIPPED,
            reason="pytest not configured or no test files detected",
        )
    return _run_command("pytest", cmd, worktree_path, opts)


def _run_mypy(worktree_path: Path, opts: ProofOptions) -> VerificationResult:
    cmd = _detect_mypy(worktree_path)
    if cmd is None:
        return VerificationResult(
            name="mypy",
            status=VerificationStatus.SKIPPED,
            reason="mypy not configured or not installed",
        )
    return _run_command("mypy", cmd, worktree_path, opts)


def _run_build(worktree_path: Path, opts: ProofOptions) -> VerificationResult:
    cmd = _detect_build(worktree_path)
    if cmd is None:
        return VerificationResult(
            name="build",
            status=VerificationStatus.SKIPPED,
            reason="build not configured (no pyproject.toml with [build-system])",
        )
    return _run_command("build", cmd, worktree_path, opts)


def _run_command(
    name: str,
    cmd: list[str],
    cwd: Path,
    opts: ProofOptions,
) -> VerificationResult:
    """Run a single verification command and return a VerificationResult."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=opts.timeout,
            shell=False,
        )
        duration = time.monotonic() - start
        stdout = _truncate(proc.stdout, opts.max_output_bytes)
        stderr = _truncate(proc.stderr, opts.max_output_bytes)
        status = (
            VerificationStatus.PASSED if proc.returncode == 0
            else VerificationStatus.FAILED
        )
        return VerificationResult(
            name=name,
            status=status,
            command=cmd,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            reason="" if status == VerificationStatus.PASSED else (
                f"exited with code {proc.returncode}"
            ),
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return VerificationResult(
            name=name,
            status=VerificationStatus.FAILED,
            command=cmd,
            exit_code=-1,
            duration_seconds=duration,
            reason=f"Command timed out after {opts.timeout}s",
        )
    except FileNotFoundError as exc:
        return VerificationResult(
            name=name,
            status=VerificationStatus.SKIPPED,
            command=cmd,
            reason=f"Executable not found: {exc}",
        )
    except OSError as exc:
        return VerificationResult(
            name=name,
            status=VerificationStatus.FAILED,
            command=cmd,
            exit_code=-1,
            reason=f"OS error: {exc}",
        )


# ---------------------------------------------------------------------------
# Re-analysis
# ---------------------------------------------------------------------------


def _do_reanalysis(
    worktree_path: Path,
    target_qname: str,
) -> ReanalysisResult:
    """
    Run the DeadCode analyzer on the isolated worktree and verify that the
    target symbol is no longer present.
    """
    try:
        result = analyze_repo(worktree_path)
    except Exception as exc:  # noqa: BLE001
        return ReanalysisResult(
            candidate_absent=False,
            error=f"Re-analysis failed: {type(exc).__name__}: {exc}",
        )

    # The re-analysis runs against the worktree, so qualified names will
    # be the same (same relative paths).  Check that the target is gone.
    present = any(
        c.qualified_name == target_qname for c in result.candidates
    )
    return ReanalysisResult(
        candidate_absent=not present,
        total_candidates_after=len(result.candidates),
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate *text* to at most *max_bytes* bytes (UTF-8 encoded)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + f"\n[... truncated at {max_bytes} bytes ...]"
