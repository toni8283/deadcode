# DeadCode

**Delete with evidence.**

DeadCode finds unused Python code and unused Python module files, gathers proof that removing them is safe using an isolated Git worktree, and lets you apply the removal with a single command — without touching your working tree until you say so.

---

## What it does

1. **Scans** your Python repository deterministically using AST analysis and module dependency resolution.
2. **Classifies** every symbol and module as `PROVABLE` (unused), `REVIEW` (uncertain / fail-closed), or `ACTIVE` (referenced / imported).
3. **Proves** that a `PROVABLE` candidate can be safely removed by:
   - Creating a temporary isolated Git worktree (`git worktree add --detach`).
   - Removing the candidate (exact AST source-range deletion for functions/classes; safe file unlinking for modules).
   - Running your existing verification checks (`pytest`, `mypy`, and build systems) inside the worktree.
   - Re-running DeadCode analysis in the worktree to confirm the candidate is absent.
4. **Applies** the proven removal to your working tree only after validating proof identity and receiving explicit confirmation.

Your current branch and working tree are never modified during scan or prove.

---

## Installation

```bash
pip install deadcode
```

Requires Python 3.11+, Git on PATH.

---

## Workflow

```bash
# 1. Find unused code
deadcode scan

# 2. Inspect a specific candidate
deadcode show DC-001

# 3. Prove it is safe to remove (isolated worktree, no branch changes)
deadcode prove DC-001

# 4. Apply the proven removal to your working tree (asks for confirmation)
deadcode apply DC-001
```

---

## Commands

### `deadcode scan [PATH]`

Scans the repository at `PATH` (default: current directory).

Outputs candidates grouped by `PROVABLE` / `REVIEW` / `ACTIVE`.
Saves scan state to `.deadcode/state.json` for subsequent commands.

```
PROVABLE  (2 candidates)

  [DC-001]  src/utils.py:42
    calculate_total  (function)
    References: 0  Tests: 0  Exported: no  Confidence: PROVABLE

  [DC-002]  src/pkg/_old_helper.py:1
    _old_helper  (module)
    References: 0  Tests: 0  Exported: no  Confidence: PROVABLE

REVIEW  (1 candidate)

  [DC-003]  src/api.py:15
    public_endpoint  (function)
    References: 0  Tests: 0  Exported: yes  Confidence: REVIEW
    Reason: Listed in __all__; public export
```

Options:
- `--active` — also show ACTIVE (referenced/imported) candidates
- `--only PROVABLE|REVIEW|ACTIVE` — filter output to one classification

### `deadcode show DC-NNN`

Shows complete structured evidence for one candidate: file, line, kind, reference counts, test references, incoming import relationships, uncertainty reasons, and all analysis evidence items.

### `deadcode prove DC-NNN`

Attempts to prove a `PROVABLE` candidate is safe to remove:

- Verifies the candidate is `PROVABLE` (refuses `REVIEW` and `ACTIVE`).
- Creates an isolated Git worktree from `HEAD` (`git worktree add --detach <tmpdir> HEAD`).
- Removes the candidate definition:
  - Functions / classes: exact AST source-range deletion with decorator preservation.
  - Modules: verifies path is within worktree and ends in `.py`, then unlinks the file.
- Executes configured verification suites inside the worktree (`pytest`, `mypy`, build tools).
- Re-analyzes the worktree codebase to confirm the candidate is genuinely absent.
- Cleans up the temporary worktree unconditionally in a `finally` block.
- Persists the proof result in `.deadcode/state.json`.
- **Never modifies your real branch or working tree.**

```
PROVING DC-002
────────────────────────────────────────────

  Candidate  :  pkg._old_helper
  Location   :  src/pkg/_old_helper.py:1
  Kind       :  module

  Creating isolated worktree        ✓
  Removing candidate                ✓  18 lines removed
  Pytest                            ✓  52 passed in 0.45s
  Mypy                              ✓  Success: no issues found
  Re-scanning repository            ✓  candidate absent

╭─────────────────────────────────────────╮
│ PROVEN SAFE TO REMOVE                   │
│                                         │
│ Run deadcode apply DC-002 to apply.     │
╰─────────────────────────────────────────╯
```

Options:
- `--timeout N` — verification command timeout in seconds (default: 120s)
- `--no-tests` — skip test verification
- `--no-typecheck` — skip typecheck verification

### `deadcode apply DC-NNN`

Applies a **previously proven** removal to your working tree:

- Refuses to apply if the candidate has not been proven.
- **Proof identity validation**: verifies that the stored proof matches the candidate's exact qualified name (`proof.candidate_qualified_name == candidate.qualified_name`), blocking execution if scan state is stale or IDs shifted.
- Shows the candidate details and prompts for explicit confirmation (`[y/N]`).
- Supports `--yes` / `-y` to skip the interactive prompt in automation.
- Unlinks module files or removes AST source ranges in place.
- Re-scans the repository to confirm removal and updates state.
- **Never commits or pushes automatically.**

---

## Candidate IDs

Candidate IDs (`DC-001`, `DC-002`, …) are **deterministic** for a given repository state:
- Candidates are sorted by `(classification_order, qualified_name, file, line)`.
- `PROVABLE` candidates always receive the lowest ID numbers, followed by `REVIEW`, then `ACTIVE`.
- IDs are persisted to `.deadcode/state.json` and remain stable across invocations as long as repository code does not change.

Add `.deadcode/` to your `.gitignore` to avoid committing transient state.

---

## Safety principles

DeadCode follows a strict fail-closed philosophy: **"Delete with evidence."**

| Rule | Implementation |
|---|---|
| Never modify working tree during scan or prove | All proof executions run inside temporary Git worktrees (`/tmp/deadcode_wt_*`) |
| Never alter the user's current branch or HEAD | Worktrees are detached (`git worktree add --detach HEAD`) |
| Never commit or push | No `git commit` or `git push` commands are ever run |
| Always clean up worktrees | Cleaned up in `finally` blocks via `git worktree remove --force` |
| Fail closed on uncertainty | When DeadCode cannot establish sufficient evidence, it classifies as `REVIEW`; `REVIEW` items cannot be proven or applied |
| Stale proof identity check | `apply` asserts `proof.candidate_qualified_name == candidate.qualified_name` |
| Strict path boundaries | Module removal verifies the target path resolves strictly within the repository/worktree root and rejects non-Python files |
| AST-based source removal | Functions/classes are located via AST matching (name + kind + line) — never naive string replacement |
| Require explicit confirmation | Interactive confirmation prompt before touching working tree files (`--yes` override) |

---

## Architecture

```
src/deadcode/
    scanner.py      Recursive .py file discovery; ignore-list filtering
    parser.py       AST visitor: defs, imports, references, __all__, dynamic import calls
    indexer.py      Aggregates per-file results into a RepoIndex
    graph.py        Import-aware reference graph and module dependency graph
    analyzer.py     Fail-closed classification for symbol and module candidates
    remover.py      AST-based source removal (functions/classes) & safe file unlinking (modules)
    prover.py       Isolated Git worktree proof lifecycle & verification runners
    state.py        Deterministic candidate IDs & atomic .deadcode/state.json persistence
    cli.py          Typer + Rich CLI interface (scan, show, prove, apply)
    models.py       Core dataclasses (SymbolDef, Candidate, AnalysisResult, FileIndex)
    proof_models.py Proof dataclasses (ProofResult, VerificationResult, RemovalResult)
```

The analysis engine is decoupled from the proof engine, and both are independent of the CLI layer.

---

## Classification

| Classification | Meaning |
|---|---|
| **PROVABLE** | Zero detected static references or incoming module dependencies, and all safety checks pass. Strong static evidence that the candidate is unused. Safe to attempt proof. |
| **REVIEW** | Static analysis encountered uncertainty: decorators, `__all__` exports, wildcard imports, dynamic access (`getattr`, `importlib`), framework hooks, script entrypoints, test-only usage, or public API modules. Requires human review. |
| **ACTIVE** | One or more static references or production incoming module dependencies were found. Candidate is in active use. |

The analyzer fails closed: when evidence is incomplete or ambiguous, it classifies candidates as `REVIEW` rather than `PROVABLE`.

---

## Tests

The test suite covers static analysis, module dependencies, worktree proofs, removal, and CLI workflows:

```bash
PYTHONPATH=src python -m pytest -q -W error
```

**257 tests passing** (0 failures, 0 errors, 0 warnings):
- `test_analyzer.py`: AST symbol classification & fail-closed triggers (47 tests)
- `test_cli.py`: CLI commands, formatting, error handling, apply prompts (52 tests)
- `test_file_candidates.py`: Python module candidate discovery & classification (32 tests)
- `test_module_deps.py`: Module dependency resolution (14 tests)
- `test_module_prover.py`: Module proof in isolated worktrees & file deletion (13 tests)
- `test_prover.py`: Function/class worktree proof lifecycle & verifiers (41 tests)
- `test_safety_hardening.py`: Safety escalation & edge case protections (13 tests)
- `test_task2.py`: Import-aware resolution & structured evidence (45 tests)

---

## Limitations

- **Python only**: Analysis currently targets Python codebases (`.py` files).
- **Supported removal kinds**:
  - Module-level functions (`FUNCTION`, `ASYNC_FUNCTION`)
  - Module-level classes (`CLASS`)
  - Python module files (`MODULE`)
  - Module-level variables and nested definitions are classified but not supported for automated removal.
- **Attribute chain depth**: Root names of attribute chains are tracked; attribute chains across modules (`import M; M.func()`) attribute usage to module `M`, leaving `func` unresolved.
- **Relative import scope**: Relative imports (`from . import mod`, `from ..mod import sym`) are resolved using package directory hierarchy. Relative imports that ascend beyond the top-level repository package or lack package context fail closed as ambiguous/external.
- **Public module conservatism**: Python module files without a leading underscore are treated as potential public API modules and classified as `REVIEW`.
- **Directory cleanup**: Unlinking an unused module file does not delete containing package directories or their `__init__.py` files.
- **Git dependency**: The proof engine requires Git on PATH to create isolated worktrees.

---

## License

MIT
