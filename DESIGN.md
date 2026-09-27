# DeadCode — Technical Design Document

## Overview

DeadCode is a standalone, deterministic static-analysis and safe dead code removal engine for Python codebases. It identifies symbols (functions, classes, variables) and entire Python modules (`SymbolKind.MODULE`) that appear to be unused, classifies them conservatively, gathers concrete proof of removal safety using isolated Git worktrees, and applies proven removals without modifying the active working tree until explicitly authorized.

**Core constraints:**
- **Deterministic and reproducible**: No AI, LLM, cloud service, or heuristic external model is used. Analysis output depends solely on repository code and configuration.
- **Fail-closed safety**: When static analysis cannot establish complete evidence of reachability, it classifies candidates as `REVIEW` rather than `PROVABLE`.
- **Zero collateral alteration**: Proof verification occurs exclusively in isolated Git worktrees. Current branches, working trees, and unstaged modifications are never touched during analysis or proof.

---

## Architecture

```
src/deadcode/
    __init__.py     Public API surface (analyze_repo, AnalysisResult, Candidate)
    models.py       Dataclasses: SymbolDef, Candidate, AnalysisResult, FileIndex, RepoIndex
    scanner.py      Recursive .py file discovery; ignore-list filtering; module name derivation
    parser.py       AST analysis per file: definitions, imports, references, __all__, dynamic imports
    indexer.py      Aggregates per-file results into a unified RepoIndex
    graph.py        Constructs symbol reference graph and module dependency graph
    analyzer.py     Fail-closed safety classification for symbols and modules
    remover.py      AST-based source removal (functions/classes) & safe file unlinking (modules)
    prover.py       Isolated Git worktree proof lifecycle & verification runners
    proof_models.py Proof dataclasses (ProofResult, VerificationResult, RemovalResult)
    state.py        Deterministic candidate ID assignment & atomic .deadcode/state.json persistence
    cli.py          Typer + Rich CLI interface (scan, show, prove, apply)
```

### Complete System Pipeline

```text
1. SCAN & PARSE:
   scan_files() ───► parse_file() × N ───► build_index() ───► RepoIndex

2. GRAPH & DEPENDENCY ANALYSIS:
   RepoIndex ───► build_graph() ───► ReferenceGraph
                                       ├── Symbol Reference Graph (DefNode ──► SymbolRef)
                                       └── Module Dependency Graph (consumer ◄──► target)

3. CLASSIFICATION:
   ReferenceGraph ───► _classify_nodes() (symbols) ──┐
                  ───► _classify_files() (modules) ──┴──► AnalysisResult

4. STATE PERSISTENCE:
   AnalysisResult ───► assign_ids() ───► save_state() ───► .deadcode/state.json

5. PROOF LIFECYCLE (deadcode prove DC-NNN):
   Candidate ──► Isolated Worktree ──► remove_candidate() ──► Verifications (pytest/mypy)
             ──► Re-analysis (verify absence) ──► Cleanup Worktree ──► record_proof()

6. APPLICATION (deadcode apply DC-NNN):
   Verified Proof Record ──► User Confirmation ──► remove_candidate() (in working tree) ──► Re-scan
```

---

## 1. Scanning (`scanner.py`)

`scan_files(root)` traverses the repository using `pathlib.Path.iterdir()` (avoiding `os.walk` to enforce strict boundary checks).

### Ignored Directories
Exact basename matches ignored at all tree depths:
```text
.git  .hg  .svn  .venv  venv  env  .env  node_modules  __pycache__
dist  build  .tox  .mypy_cache  .pytest_cache  .ruff_cache  .nox
site-packages
```
Directories ending in `.egg-info` or `.dist-info` are likewise skipped. Symbolic links are never followed.

### Module Name Derivation (`derive_module_name`)
- Detects whether `src/` exists under the repository root and contains Python files (standard `src` layout). If present, `src/` serves as the package root; otherwise, the repository root is used.
- Converts relative file paths to dotted module names: directory separators become dots, `.py` is stripped, and `__init__.py` collapses to its parent package name (e.g. `src/pkg/utils.py` → `pkg.utils`; `src/pkg/__init__.py` → `pkg`).

---

## 2. AST Parsing (`parser.py`)

`parse_file(file_path, module_name)` parses single `.py` files using the Python standard library `ast` module. It never raises unhandled exceptions: syntax errors set `parse_error` on the returned `FileIndex`.

### Definitions Extracted

| AST Node | SymbolKind | Notes |
|---|---|---|
| `FunctionDef` | `FUNCTION` | Module-level and nested functions |
| `AsyncFunctionDef` | `ASYNC_FUNCTION` | Asynchronous coroutines |
| `ClassDef` | `CLASS` | Classes and nested classes |
| `Assign` (top-level) | `VARIABLE` | Module-level assignments |
| `AnnAssign` (top-level) | `VARIABLE` | Module-level annotated assignments |

Each `SymbolDef` captures:
- `qualified_name`: scope-stack aware name (`module.Class.method`)
- `is_private`: True if the name begins with `_`
- `has_decorator`: True if one or more decorators wrap the definition
- `is_dunder`: True for `__dunder__` names
- `in_all`: True / False based on static `__all__` list extraction; None if no `__all__` exists
- `base_classes`: list of string names of inherited base classes
- `contains_dynamic_call`: True if dynamic functions (`getattr`, `eval`, `globals`, `importlib`) are called within the definition body

### Imports Extracted
Every `import X` and `from X import Y [as Z]` statement produces an `ImportRecord`:
- `module`: Target module name string
- `names`: List of imported names (empty for bare `import M`)
- `alias`: Local alias if `as` was specified
- `is_wildcard`: True for `from M import *`
- `level`: Number of leading dots for relative imports (`0` for absolute, `1` for `.`, `2` for `..`)

### References Extracted
Every `ast.Name(ctx=Load)` node is recorded as a `SymbolRef`:
- **Attribute access**: The leftmost root name is recorded (`utils` from `utils.helper()`).
- **Decorators**: Added with `context="decorator"`.
- **Base classes**: Added with `context="base_class"`.
- **Dynamic string references**: First argument strings to `getattr()`, `hasattr()`, etc. are captured with `context="dynamic_call"`.
- **Dynamic imports**: `importlib.import_module(arg)` and `__import__(arg)` calls are inspected. Literal string arguments are added to `FileIndex.dynamic_import_targets`; computed or non-literal arguments set `FileIndex.has_unresolved_dynamic_import = True`.
- **Script entry points**: Detects `if __name__ == "__main__":` blocks (`has_main_block`) and shebang lines (`has_shebang`).

---

## 3. Indexing (`indexer.py`)

`build_index(repo_root)` coordinates file scanning and parsing, assembling a `RepoIndex`:
- `definitions`: `qualified_name → SymbolDef`
- `name_to_defs`: `bare_name → list[SymbolDef]` (cross-file bare-name lookup)
- `files`: list of all indexed `FileIndex` objects
- Collects parse errors per file without interrupting repository analysis.

---

## 4. Reference Graph & Module Dependency Analysis (`graph.py`)

`build_graph(index)` builds a unified `ReferenceGraph` modeling both symbol-level references and module-level dependency relationships.

### Symbol Reference Graph
- Each `SymbolDef` is wrapped in a `DefNode`.
- References are linked to definitions using import-aware resolution:
  - **Pinned via `from M import N`**: If the consumer file imports `N` from local module `M`, usage of `N` is credited exclusively to `M.N`.
  - **Attribute root via `import M`**: If `import M` is present, usage of `M` in `M.func()` attributes credit to module `M`.
  - **Bare fallback**: Unimported same-file or global calls look up candidates via `name_to_nodes`. If multiple definitions share the same bare name without disambiguating imports, all matching definitions are flagged `uncertain_due_to_ambiguous_import`.
- **Test-file separation**: References from test files (`test_*.py`, `*_test.py`, or inside `tests/` directories) are flagged with `is_from_test = True` and tracked separately (`DefNode.test_references`).

### Module Dependency Graph
`ReferenceGraph` maintains a bidirectional module-level dependency graph:
- `module_dependencies`: `consumer_module → set[target_modules]`
- `incoming_module_dependencies`: `target_module → set[consumer_modules]`
- Self-dependencies are filtered out.

#### Import Resolution Precedence (`_build_import_map`)
When resolving `from M import N`:
1. **Target module definition check**: First checks whether `N` is an actual symbol defined in module `M` (`target_fi`). If defined in `M`, the module dependency is `M` and the symbol is pinned to `M.N`.
2. **Indexed submodule check**: Only if `N` is not defined in module `M`, checks whether `f"{M}.{N}"` exists as an indexed repository submodule. If it exists, records a module dependency on the submodule `M.N`.
3. **External / fallback check**: If neither, marks `N` as external or falls back to bare-name symbol matching if `M` is a local re-exporter.

#### Relative Module Resolution (`_resolve_relative_module`)
- Resolves relative imports (`level > 0`) using the consumer file's package depth.
- `from . import mod` within `pkg.sub` resolves to `pkg.mod`.
- `from ..other import mod` ascends package parts accordingly.
- Relative imports that attempt to ascend beyond the top-level package or lack package context return `None` and fail closed.

---

## 5. Classification Engine (`analyzer.py`)

DeadCode produces two categories of candidates:
1. **Symbol Candidates** (`SymbolKind.FUNCTION`, `ASYNC_FUNCTION`, `CLASS`, `VARIABLE`)
2. **Module Candidates** (`SymbolKind.MODULE`)

### 5.1 Symbol Classification (`_classify_node`)

#### Symbol REVIEW Triggers (Fail-Closed Escalation)
Any of the following causes an unreferenced symbol to be classified as `REVIEW`:
1. **Dunder names**: Names in `_IMPLICIT_DUNDERS` or `is_dunder = True` (implicitly called by runtime).
2. **Decorators**: `has_decorator = True` (framework registration, routing, hooks).
3. **Public export**: Listed in module `__all__`.
4. **Wildcard uncertainty**: Symbol defined in a module targeted by wildcard import, or consumer file uses wildcard imports.
5. **Dynamic reference**: Named in dynamic call strings (`getattr`, `eval`, `globals`).
6. **Internal dynamic calls**: Symbol body itself invokes dynamic functions.
7. **Parse error in file**: File had syntax errors.
8. **Framework naming pattern**: Names matching `_FRAMEWORK_PATTERNS` (`main`, `register`, `route`, `app`, `setup`, etc.).
9. **Visitor / Test methods**: `visit_*` methods in AST visitors, or lifecycle methods (`setUp`, `tearDown`) in `unittest.TestCase`.

#### Symbol Decision Table
| References Found | REVIEW Triggers Present | Classification |
|---|---|---|
| Positive production references | Any / None | **ACTIVE** |
| Zero references | One or more | **REVIEW** |
| Zero references | None | **PROVABLE** |

---

### 5.2 Module Classification (`_classify_files`)

Every indexed `.py` file is evaluated as a potential `SymbolKind.MODULE` candidate.

#### Module REVIEW Triggers
A module candidate is escalated to `REVIEW` if any of the following apply:
1. **File parse error**: File had syntax or encoding errors.
2. **Test infrastructure / discovery**:
   - `conftest.py` (`conftest_discovery`)
   - Defines a `unittest.TestCase` class (`unittest_discovery`)
   - Located in test directory or matches test naming patterns (`test_infrastructure`)
3. **Package initialization / execution entry points**:
   - `__init__.py` (`package_initialization`)
   - `__main__.py` (`package_main_entrypoint`)
4. **Script entry points**: Contains `if __name__ == "__main__":` or shebang line (`script_entrypoint`).
5. **Framework conventions**: Named `setup.py`, `manage.py`, `wsgi.py`, `asgi.py`, `noxfile.py`, `tasks.py`, `fabfile.py` (`framework_convention`).
6. **Configuration entry points**: Referenced in `pyproject.toml` (`[project.scripts]`, `[project.gui-scripts]`, `[project.entry-points.*]`) (`config_entrypoint`).
7. **Dynamic import target**: String literal target of `importlib.import_module()` or `__import__()` (`dynamic_import`).
8. **Unresolved dynamic imports**: Any non-literal dynamic import exists in the repository or file (`unresolved_dynamic_import`).
9. **Namespace packages**: File resides in a PEP 420 namespace package missing `__init__.py` (`namespace_package`).
10. **Public API modules**: Module file stem does not start with `_` (`public_api_module`).
11. **Parent package `__all__` export**: Module name is listed in the parent package's `__all__` list (`exported_in_all`).

#### Module Decision Precedence
1. **Parse error** → `REVIEW`
2. **Production incoming module dependency** → `ACTIVE`
3. **Test-only incoming module dependency** → `REVIEW`
4. **Any safety trigger present** → `REVIEW`
5. **Zero incoming dependencies + internal private module (`_*.py`) + passes all safety checks** → `PROVABLE`

---

## 6. Safe Removal Engine (`remover.py`)

`remove_candidate(candidate, worktree_root)` performs candidate removal inside a specified root.

### Supported Kinds
- `SymbolKind.FUNCTION`
- `SymbolKind.ASYNC_FUNCTION`
- `SymbolKind.CLASS`
- `SymbolKind.MODULE`

### Path Safety Boundaries
- Resolves `worktree_root` to an absolute path (`wt_root`).
- Remaps candidate file paths into the worktree using `_remap_to_worktree()`.
- Validates that `wt_file.relative_to(wt_root)` succeeds, refusing paths or symlinks resolving outside the worktree root.
- Requires target files to end in `.py`, exist, and be regular files (`is_file()`).

### Module Removal (`SymbolKind.MODULE`)
- Reads source content to calculate exact line count.
- Unlinks the file using `Path.unlink()`.
- Returns the line count removed.
- Does not delete parent directories or `__init__.py` files.

### Symbol Removal (`FUNCTION`, `ASYNC_FUNCTION`, `CLASS`)
- Parses source into an AST using `ast.parse()`.
- Locates the definition node matching `name`, `kind`, and `lineno`.
- Computes exact line range using `_definition_line_range()`, taking leading decorators into account via `_effective_start_line()`.
- Slices the lines out in memory, preserving surrounding blank line formatting.
- Re-parses modified code to verify syntax validity before writing back to disk.
- Returns lines removed.

---

## 7. Proof Engine (`prover.py`)

`prove_candidate(repo_root, candidate, opts)` gathers concrete verification proof that a `PROVABLE` candidate can be safely removed.

### Proof Lifecycle
1. **Eligibility verification**: Candidate must be `PROVABLE` and belong to `_SUPPORTED_KINDS`. `REVIEW` and `ACTIVE` candidates return `ProofStatus.BLOCKED`.
2. **Git repository verification**: Confirms `git` is available on PATH and `repo_root` contains a valid Git repository.
3. **Isolated worktree creation**:
   ```bash
   git worktree add --detach <system_tmpdir>/deadcode_wt_<id> HEAD
   ```
4. **Candidate removal**: Calls `remove_candidate(candidate, worktree_path)` to modify only the isolated worktree copy.
5. **Verification execution**:
   - **`pytest`**: Probes virtual environment binaries or falls back to system `python -m pytest`.
   - **`mypy`**: Probes `mypy.ini`, `.mypy.ini`, `setup.cfg`, `pyproject.toml` for `[tool.mypy]`; skips if unconfigured.
   - **Build**: Probes `pyproject.toml` for `[build-system]`; runs build check if enabled in options.
   - **Extra commands**: Executes user-specified verification commands.
   - Non-zero exit code halts proof and marks status `ProofStatus.FAILED`.
6. **Re-analysis confirmation**: Re-runs `analyze_repo(worktree_path)`. Confirms that `candidate.qualified_name` is absent from all discovered candidates.
7. **Worktree cleanup**: In a `finally` block, forces worktree removal:
   ```bash
   git worktree remove --force <worktree_path>
   ```
8. **Result persistence**: Returns a `ProofResult` containing status (`PROVEN`, `FAILED`, or `BLOCKED`), captured outputs, lines removed, and verification details.

---

## 8. State Persistence & Proof Hardening (`state.py`, `cli.py`)

### Deterministic Candidate IDs (`assign_ids`)
- Candidate IDs (`DC-001`, `DC-002`, …) are assigned by sorting candidates:
  ```python
  key = (classification_order, candidate.qualified_name, candidate.file, candidate.line)
  ```
  where `classification_order` places `PROVABLE` first (`0`), `REVIEW` second (`1`), and `ACTIVE` third (`2`).
- Sequential numbers are formatted as `DC-{i:03d}`.
- IDs are completely deterministic for a given codebase state.

### State File (`.deadcode/state.json`)
- Writes are atomic: writes to a temporary file in `.deadcode/` before replacing `state.json`.
- Stores scan metadata, all candidates, and proof records.

### Proof Identity Hardening (`cli.py`)
In `deadcode apply DC-NNN`:
- The stored proof record is loaded from `.deadcode/state.json`.
- The CLI verifies that:
  ```python
  proof.get("candidate_qualified_name") == candidate.qualified_name
  ```
- If the repository was edited or re-scanned such that `DC-NNN` was reassigned to a different candidate, the command aborts with:
  ```text
  BLOCKED: Stored proof for DC-NNN does not match candidate '<candidate_qualified_name>'.
  ```
- Exits with code `2` without touching the filesystem.

---

## 9. Test Suite Verification

The test suite validates static analysis, module dependencies, worktree proofs, remover operations, and CLI flows:

```bash
PYTHONPATH=src python -m pytest -q -W error
```

**257 tests passing** (0 failures, 0 errors, 0 warnings):
- `tests/test_analyzer.py` (47 tests): AST symbol classification and fail-closed rules
- `tests/test_cli.py` (52 tests): CLI command parsing, table output, proof formatting, apply flow
- `tests/test_file_candidates.py` (32 tests): Module candidate discovery, entrypoints, and safety triggers
- `tests/test_module_deps.py` (14 tests): Absolute, relative, submodule, and wildcard module dependencies
- `tests/test_module_prover.py` (13 tests): Module worktree proof, file unlinking, test/mypy failure handling
- `tests/test_prover.py` (41 tests): Function/class worktree proof lifecycle and verification drivers
- `tests/test_safety_hardening.py` (13 tests): Dynamic call escalation, framework conventions, dunders
- `tests/test_task2.py` (45 tests): Import-aware reference resolution and structured evidence

---

## 10. Extension Points

| Capability | Module to Extend | Approach |
|---|---|---|
| New file / language support | `scanner.py`, `parser.py` | Add file extensions and specialized AST visitors |
| Deeper attribute tracking | `graph.py` | Track multi-part attribute chains across imported module namespaces |
| Additional framework conventions | `analyzer.py` | Add framework pattern definitions or configuration scanners |
| Additional verification tools | `prover.py` | Add auto-detection and execution helpers in `_run_verifications()` |

---

## 11. Current Limitations

1. **Python Only**: Analysis targets standard `.py` files.
2. **Removable Kinds**: Automated removal supports module-level functions, classes, and `.py` module files. Variables and nested definitions are not removable.
3. **Attribute Chains Beyond Root**: For `import M; M.func()`, usage is credited to module `M`, leaving `func` unresolved.
4. **Relative Imports**: Relative imports are resolved relative to the containing package hierarchy; imports ascending beyond the top-level repository package fail closed.
5. **Public Module Conservatism**: Non-private module files (stem not beginning with `_`) are classified as `REVIEW` to protect public library APIs.
6. **Directory Cleanup**: Module removal unlinks the `.py` file; parent directories and `__init__.py` files are not deleted.
7. **Git Requirement**: The proof engine requires Git on PATH to create detached worktrees.
