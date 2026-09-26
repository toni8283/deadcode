# DeadCode

**Delete with evidence.**

DeadCode finds unused Python code, gathers proof that removing it is safe using an isolated Git worktree, and lets you apply the removal with a single command — without touching your working tree until you say so.

---

## What it does

1. **Scans** your Python repository deterministically using AST analysis.
2. **Classifies** every symbol as `PROVABLE` (unused), `REVIEW` (uncertain), or `ACTIVE` (referenced).
3. **Proves** that a `PROVABLE` candidate can be safely removed by:
   - Creating a temporary isolated Git worktree.
   - Removing the symbol using exact AST-based source-range deletion.
   - Running your existing tests, typechecker, and build inside the worktree.
   - Re-running the DeadCode analysis to confirm the symbol is gone.
4. **Applies** the proven removal to your real working tree only on explicit confirmation.

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
Saves a state file to `.deadcode/state.json` for use by other commands.

```
PROVABLE  (3 candidates)

  [DC-001]  src/utils.py:42
    calculate_total  (function)
    References: 0  Tests: 0  Exported: no  Confidence: PROVABLE

  [DC-002]  src/payments.py:18
    format_receipt  (function)
    References: 0  Tests: 0  Exported: no  Confidence: PROVABLE
```

Options:
- `--active` — also show ACTIVE (referenced) symbols
- `--only PROVABLE|REVIEW|ACTIVE` — filter to one class

### `deadcode show DC-NNN`

Shows the complete evidence for one candidate: file, line, kind, references, test references, import relationships, uncertainty reasons, and all analysis evidence items.

### `deadcode prove DC-NNN`

Attempts to prove a `PROVABLE` candidate is safe to remove.

- Creates an isolated Git worktree from `HEAD` (`--detach`).
- Removes the candidate using exact AST source-range deletion.
- Runs tests (`pytest`), typecheck (`mypy`), and re-analysis inside the worktree.
- Reports `PROVEN SAFE TO REMOVE` or `PROOF FAILED` with the failing step.
- Cleans up the worktree unconditionally.
- **Never modifies your real branch or working tree.**

```
PROVING DC-001
────────────────────────────────────────────

  Candidate  :  calculate_total
  Location   :  src/utils.py:42
  Kind       :  function

  Creating isolated worktree        ✓
  Removing candidate                ✓  3 lines removed
  Pytest                            ✓  48 passed in 0.12s
  Mypy                              –  SKIPPED (not configured)
  Re-scanning repository            ✓  candidate absent

╭─────────────────────────────────────────╮
│ PROVEN SAFE TO REMOVE                   │
│                                         │
│ Run deadcode apply DC-001 to apply.     │
╰─────────────────────────────────────────╯
```

Options:
- `--timeout N` — verification command timeout (default: 120s)
- `--no-tests` — skip test verification
- `--no-typecheck` — skip typecheck verification

### `deadcode apply DC-NNN`

Applies a **previously proven** candidate removal to your working tree.

- Refuses to apply if the candidate has not been proven.
- Shows the exact candidate and asks for explicit confirmation (`[y/N]`).
- Uses `--yes` / `-y` to skip the prompt in scripts.
- Re-runs the analysis after removal to confirm the symbol is gone.
- **Does not commit or push automatically.**

---

## Candidate IDs

Candidate IDs (`DC-001`, `DC-002`, …) are **deterministic** for a given repository state. The same analysis output always produces the same IDs. IDs are stored in `.deadcode/state.json` and remain stable across processes.

Add `.deadcode/` to your `.gitignore` to avoid committing transient state.

---

## Safety principles

| Rule | Implementation |
|---|---|
| Never modify the working tree during scan or prove | All proof work happens in `/tmp/deadcode_wt_*` |
| Never change the user's branch | `git worktree add --detach HEAD` |
| Never commit or push | No `git commit` or `git push` calls |
| Always clean up the worktree | `finally` block with `git worktree remove --force` |
| Fail closed on uncertainty | REVIEW symbols cannot be proven or applied |
| Require explicit confirmation to apply | Interactive `[y/N]` prompt (override with `--yes`) |
| AST-based removal only | No naive text replacement; verifies name + kind + line |

---

## Architecture

```
src/deadcode/
    scanner.py      Recursive .py file discovery; ignore-list filtering
    parser.py       AST visitor: defs, imports, references, __all__
    indexer.py      Aggregates per-file results into a RepoIndex
    graph.py        Import-aware reference graph (def → refs)
    analyzer.py     Fail-closed classification: PROVABLE / REVIEW / ACTIVE
    remover.py      AST-based exact source-range removal
    prover.py       Isolated Git worktree proof lifecycle
    state.py        Deterministic candidate IDs; .deadcode/state.json
    cli.py          Typer + Rich user interface
    models.py       Core dataclasses (SymbolDef, Candidate, AnalysisResult)
    proof_models.py Proof dataclasses (ProofResult, VerificationResult)
```

The analysis engine is completely separate from the proof engine. Both are separate from the CLI. The CLI is thin: it calls the engine APIs and renders results.

---

## Classification

| Classification | Meaning |
|---|---|
| **PROVABLE** | Zero static references, no uncertainty factors. Safe to attempt proof. |
| **REVIEW** | Static analysis found uncertainty: decorator, `__all__`, wildcard import, dynamic access, dunder, framework pattern. Human review required. |
| **ACTIVE** | One or more static references found. Symbol is in use. |

The analyzer fails closed: when in doubt, it classifies as REVIEW rather than PROVABLE.

---

## Tests

```
133 tests passing
  Task 1: 47  (analysis engine)
  Task 2: 45  (import-aware resolution + structured evidence)
  Task 3: 41  (proof engine)
```

Run the suite:

```bash
pip install -e ".[dev]"
pytest
```

---

## Limitations

- Python only (JavaScript/TypeScript support is planned).
- Only module-level functions and classes can be removed (not variables or nested definitions).
- Attribute-chain resolution (`import M; M.func()`) is not yet implemented — `func` remains unresolved (conservative).
- Relative imports are not resolved (treated as uncertain).
- Requires Git to be installed for `prove` and `apply`.

---

## License

MIT
