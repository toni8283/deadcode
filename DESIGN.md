# DeadCode — Technical Design Document

## Overview

DeadCode is a standalone, deterministic static-analysis engine for Python
codebases. It identifies symbols (functions, classes, variables) that appear
to be unused, classifies the confidence of that conclusion, and surfaces the
evidence so a developer or downstream tool can make an informed decision.

**Core constraint:** No AI, LLM, cloud service, or external model is used at
any point. All analysis is deterministic and reproducible.

---

## Architecture

```
src/deadcode/
    __init__.py    Public API surface
    models.py      Dataclasses: all shared data structures
    scanner.py     File discovery – recursive directory walk
    parser.py      AST analysis of a single .py file
    indexer.py     Aggregates per-file results into a RepoIndex
    graph.py       Builds the reference graph (def → refs)
    analyzer.py    Classifies every symbol; exposes analyze_repo()
```

The pipeline runs in strict order:

```
scan_files()  →  parse_file() × N  →  build_index()  →  build_graph()  →  classify
```

---

## 1. Scanning (`scanner.py`)

`scan_files(root)` walks the directory tree with `pathlib.Path.iterdir()`
(not `os.walk`) to give precise control over which directories are entered.

**Ignored directory names** (applied as exact basename matches at every tree
level):

```
.git  .hg  .svn  .venv  venv  env  .env  node_modules  __pycache__
dist  build  .tox  .mypy_cache  .pytest_cache  .ruff_cache  .nox
site-packages
```

Directories whose names end in `.egg-info` or `.dist-info` are also skipped.

Symbolic links are never followed (prevents infinite loops).

OS errors (permission denied, etc.) are caught per-directory and returned as
`(path, message)` pairs; they never abort the scan.

**Module name derivation** (`derive_module_name`):

The package root is detected by checking whether a `src/` directory exists
under the repo root and contains Python files (the `src` layout). If so,
`src/` is used as the package root; otherwise the repo root itself is used.

A file's path relative to the package root is converted to a dotted module
name: path separators become dots, `.py` is stripped, and `__init__` collapses
to its parent package name.

---

## 2. AST Parsing (`parser.py`)

`parse_file(file_path, module_name)` never raises. It returns a `FileIndex`
with `parse_error` set if the file cannot be read or parsed.

The `_FileVisitor(ast.NodeVisitor)` subclass walks the AST and collects:

### Definitions

| AST node | Stored as |
|---|---|
| `FunctionDef` | `SymbolDef(kind=FUNCTION)` |
| `AsyncFunctionDef` | `SymbolDef(kind=ASYNC_FUNCTION)` |
| `ClassDef` | `SymbolDef(kind=CLASS)` |
| `Assign` (top-level) | `SymbolDef(kind=VARIABLE)` |
| `AnnAssign` (top-level) | `SymbolDef(kind=VARIABLE)` |

Each definition records:
- **`qualified_name`** — `module.ClassName.method_name` (scope-stack aware)
- **`is_private`** — True if name starts with `_`
- **`has_decorator`** — True if one or more decorators are present
- **`is_dunder`** — True for `__foo__` names
- **`in_all`** — True/False/None based on `__all__` detection

### `__all__` detection

If a module-level `Assign` targets `__all__` with a static list/tuple of
string literals, all names are extracted and used to set `defn.in_all` on
matching definitions. A dynamic `__all__` (non-string elements, dict, call,
etc.) yields `None` for all definitions, which is treated as uncertain.

### Imports

Every `import X` and `from X import Y [as Z]` statement is recorded as an
`ImportRecord`. Wildcard imports (`from pkg import *`) set
`has_wildcard_import = True` on the `FileIndex`.

### References

Every `ast.Name(ctx=Load)` node is recorded as a `SymbolRef`. Additionally:

- **Attribute chains** — the leftmost (root) name is extracted
- **Decorators** — added as `context="decorator"` refs
- **Base classes** — added as `context="base_class"` refs
- **Dynamic call arguments** — if `getattr(obj, "name")` appears, the string
  `"name"` is added as a `context="dynamic_call"` ref
- **`global` / `nonlocal` statements** — recorded as refs

The parser intentionally over-collects references (all `Name(Load)` nodes)
rather than under-collecting them. False positives in the reference set cause
us to mark a live symbol as ACTIVE, which is safe. False negatives (missing a
reference) would risk deleting live code, which is not safe.

---

## 3. Indexing (`indexer.py`)

`build_index(repo_root)` runs `scan_files()` then `parse_file()` for each
discovered `.py` file. Results are merged into a `RepoIndex`:

- `RepoIndex.definitions` — `qualified_name → SymbolDef` (last writer wins on
  collision)
- `RepoIndex.name_to_defs` — `bare_name → list[SymbolDef]` (all defs sharing a
  name across files)

Parse errors are collected in the error list and do not abort indexing of
other files.

---

## 4. Reference Graph (`graph.py`)

`build_graph(index)` constructs a `ReferenceGraph` where each `DefNode` wraps
a `SymbolDef` and accumulates the `SymbolRef` objects that appear to reference
it.

### Resolution strategy

Each `SymbolRef.name` (a bare name) is looked up in a reverse map
`name → list[DefNode]`. If the name matches one or more definitions, those
definitions receive the reference.

We intentionally do **not** attempt full Python name resolution (scope chains,
import aliasing, `sys.path` traversal). Such resolution would require
executing the code or maintaining a full type-inference engine, both of which
violate the determinism and no-external-dependency constraints. The conservative
approach means some symbols receive references they should not (false ACTIVE),
but no live symbol is incorrectly classified PROVABLE.

### Uncertainty flags

Two boolean flags on `DefNode` capture dynamic uncertainty:

| Flag | Set when |
|---|---|
| `uncertain_due_to_wildcard` | A `from pkg import *` sources from the same module, OR the consumer file has a wildcard import |
| `uncertain_due_to_dynamic` | A `getattr`/`eval`/`exec`/`globals()`/etc. call references the symbol's name |

---

## 5. Classification (`analyzer.py`)

`_classify_node()` applies the following ordered rules:

### REVIEW triggers (fail-closed escalation)

Any one of these conditions causes the symbol to be classified **REVIEW**
unless positive references exist:

1. Symbol is a dunder (`__init__`, `__str__`, etc.) — implicitly called by Python
2. Symbol carries a decorator — possible framework registration
3. Symbol is listed in `__all__` — may be imported externally
4. `uncertain_due_to_wildcard` — wildcard import obscures reachability
5. `uncertain_due_to_dynamic` — dynamic access could reference this symbol
6. The source file has a wildcard import — namespace is uncertain
7. The source file had a parse error — analysis is incomplete
8. Symbol name matches a framework pattern (`main`, `route`, `register`, etc.)

### Decision table

| References found? | REVIEW triggers? | Classification |
|---|---|---|
| Yes | Any | **ACTIVE** |
| No | Any | **REVIEW** |
| No | None | **PROVABLE** |

ACTIVE always wins — if we found a reference, the symbol is live regardless
of uncertainty flags.

### Symbols excluded from candidacy

- `__all__` variable itself
- Import-kind symbols in `__init__.py` files (these are typically re-exports)

---

## 6. Why the Analyzer Fails Closed

Python's dynamic features make it impossible to enumerate all possible
references at static analysis time:

- `getattr(obj, name)` where `name` is a runtime string
- `from pkg import *` introduces unknown names
- `importlib.import_module(name)` where `name` is computed
- Decorators can register functions in global registries
- Metaclasses and descriptors can synthesise attribute access
- Entry points in `pyproject.toml` / `setup.cfg` reference dotted paths
- `eval` / `exec` can reference any name
- String-based plugin systems (Django apps, pytest plugins, Celery tasks)

Each of these patterns means that a symbol with zero *detected* references
might still be referenced *at runtime*. Deleting such a symbol would introduce
a bug that is invisible at static analysis time.

The fail-closed rule encodes this conservatism explicitly: **when in doubt,
classify REVIEW**. Only symbols with zero references AND no uncertainty flags
whatsoever are promoted to PROVABLE. This means DeadCode may produce false
REVIEW results (over-reporting uncertainty), but it will never silently
produce a false PROVABLE result that leads to incorrect deletion of live code.

---

## 7. Public API

```python
from deadcode import analyze_repo, AnalysisResult

result: AnalysisResult = analyze_repo("/path/to/project")

for candidate in result.provable:
    print(f"PROVABLE  {candidate.qualified_name}  {candidate.file}:{candidate.line}")

for candidate in result.review:
    print(f"REVIEW    {candidate.qualified_name}")
```

`AnalysisResult` is fully JSON-serialisable:

```python
import dataclasses, json
print(json.dumps(dataclasses.asdict(result), indent=2, default=str))
```

---

## 8. Extension Points

The modular design allows future additions without touching core logic:

| Concern | Module to extend |
|---|---|
| Support new file types | `scanner.py` + new `parse_*` function |
| Richer reference resolution (imports) | `graph.py` `_resolve_ref` |
| Test-file awareness | `analyzer.py` `_should_skip` |
| Entry-point / pyproject.toml awareness | `indexer.py` pre-pass |
| Export detection beyond `__all__` | `parser.py` + `models.py` |
