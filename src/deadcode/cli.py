"""
cli.py – DeadCode command-line interface.

Commands
--------
    deadcode scan [PATH]
    deadcode show DC-NNN
    deadcode prove DC-NNN
    deadcode apply DC-NNN

All commands are thin wrappers around the engine modules.  Business logic
stays in analyzer.py / prover.py / remover.py / state.py.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from deadcode.analyzer import analyze_repo
from deadcode.models import SafetyClassification
from deadcode.proof_models import ProofStatus, VerificationStatus
from deadcode.prover import ProofOptions, prove_candidate
from deadcode.remover import RemovalError, remove_candidate
from deadcode.state import (
    assign_ids,
    get_proof,
    is_proven,
    load_candidate,
    load_state,
    record_proof,
    save_state,
    state_path,
)

# ---------------------------------------------------------------------------
# App + console setup
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="deadcode",
    help="Find and safely remove unused Python code — with proof.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

# ---------------------------------------------------------------------------
# Colour / style constants
# ---------------------------------------------------------------------------

STYLE_PROVABLE = "bold red"
STYLE_REVIEW = "bold yellow"
STYLE_ACTIVE = "green"
STYLE_OK = "bold green"
STYLE_FAIL = "bold red"
STYLE_SKIP = "dim"
STYLE_HEADER = "bold white"
ICON_OK = "[bold green]✓[/]"
ICON_SKIP = "[dim]–[/]"
ICON_FAIL = "[bold red]✗[/]"
ICON_WARN = "[bold yellow]![/]"


# ---------------------------------------------------------------------------
# Helper: resolve repo root
# ---------------------------------------------------------------------------


def _resolve_repo(path_arg: Optional[str]) -> Path:
    """Return the resolved repository root path."""
    if path_arg:
        p = Path(path_arg).resolve()
    else:
        p = Path.cwd()
    if not p.exists():
        err_console.print(f"[red]Error:[/] Path does not exist: {p}")
        raise typer.Exit(code=1)
    if not p.is_dir():
        err_console.print(f"[red]Error:[/] Path is not a directory: {p}")
        raise typer.Exit(code=1)
    return p


def _rel(file_path: str, repo_root: Path) -> str:
    """Return a repo-relative display path, or the original if not relative."""
    try:
        return str(Path(file_path).relative_to(repo_root))
    except ValueError:
        return file_path


# ---------------------------------------------------------------------------
# SCAN
# ---------------------------------------------------------------------------


@app.command()
def scan(
    path: Annotated[
        Optional[str],
        typer.Argument(help="Repository path to scan (default: current directory)"),
    ] = None,
    show_active: Annotated[
        bool,
        typer.Option("--active", help="Also show ACTIVE (referenced) symbols"),
    ] = False,
    only: Annotated[
        Optional[str],
        typer.Option("--only", help="Filter to one class: PROVABLE, REVIEW, or ACTIVE"),
    ] = None,
) -> None:
    """Scan a repository and display unused code candidates."""
    repo_root = _resolve_repo(path)

    console.print()
    console.rule("[bold]DeadCode[/]")
    console.print(f"  Repository : [cyan]{repo_root}[/]")
    console.print()

    with console.status("Scanning…", spinner="dots"):
        result = analyze_repo(repo_root)

    id_map = assign_ids(result.candidates)
    save_state(
        repo_root,
        id_map,
        files_scanned=result.files_scanned,
        total_definitions=result.total_definitions,
    )

    # Summary line
    console.print(
        f"  Files scanned      : [bold]{result.files_scanned}[/]\n"
        f"  Symbols indexed    : [bold]{result.total_definitions}[/]\n"
        f"  Candidates found   : [bold]{len(result.candidates)}[/]"
    )
    console.print()

    # Group by classification
    groups = {
        SafetyClassification.PROVABLE: [],
        SafetyClassification.REVIEW: [],
        SafetyClassification.ACTIVE: [],
    }
    for dc_id, cand in id_map.items():
        groups[cand.classification].append((dc_id, cand))

    filter_class: SafetyClassification | None = None
    if only:
        try:
            filter_class = SafetyClassification(only.upper())
        except ValueError:
            err_console.print(f"[red]Error:[/] Unknown class '{only}'. Use PROVABLE, REVIEW, or ACTIVE.")
            raise typer.Exit(code=1)

    # ---- PROVABLE ----
    if filter_class in (None, SafetyClassification.PROVABLE):
        provable = groups[SafetyClassification.PROVABLE]
        if provable:
            console.print(f"[{STYLE_PROVABLE}]PROVABLE[/]  ({len(provable)} candidates)\n")
            for dc_id, cand in provable:
                _print_provable_card(dc_id, cand, repo_root)
        else:
            console.print(f"[{STYLE_PROVABLE}]PROVABLE[/]  (none)\n")

    # ---- REVIEW ----
    if filter_class in (None, SafetyClassification.REVIEW):
        review = groups[SafetyClassification.REVIEW]
        if review:
            console.print(f"[{STYLE_REVIEW}]REVIEW[/]    ({len(review)} candidates)\n")
            for dc_id, cand in review:
                _print_review_card(dc_id, cand, repo_root)
        else:
            console.print(f"[{STYLE_REVIEW}]REVIEW[/]    (none)\n")

    # ---- ACTIVE ----
    if show_active or filter_class == SafetyClassification.ACTIVE:
        active = groups[SafetyClassification.ACTIVE]
        if active:
            console.print(f"[{STYLE_ACTIVE}]ACTIVE[/]    ({len(active)} candidates)\n")
            for dc_id, cand in active:
                _print_active_card(dc_id, cand, repo_root)

    state_file = state_path(repo_root)
    console.print(f"\n  State saved : [dim]{state_file}[/]")
    console.print(
        "  Run [bold]deadcode show DC-NNN[/] to inspect a candidate.\n"
        "  Run [bold]deadcode prove DC-NNN[/] to attempt proof.\n"
    )


def _print_provable_card(dc_id: str, cand, repo_root: Path) -> None:
    rel_file = _rel(cand.file, repo_root)
    exported = "yes" if cand.is_exported else "no" if cand.is_exported is False else "—"
    console.print(
        f"  [{STYLE_PROVABLE}][{dc_id}][/]  {rel_file}:{cand.line}\n"
        f"    [bold]{cand.symbol.name}[/]  ({cand.symbol.kind.value})\n"
        f"    References: {cand.ref_count}  Tests: {cand.test_ref_count}  Exported: {exported}"
        f"  Confidence: [bold red]PROVABLE[/]\n"
    )


def _print_review_card(dc_id: str, cand, repo_root: Path) -> None:
    rel_file = _rel(cand.file, repo_root)
    reasons = ", ".join(cand.uncertainty_reasons) or "—"
    console.print(
        f"  [{STYLE_REVIEW}][{dc_id}][/]  {rel_file}:{cand.line}\n"
        f"    [bold]{cand.symbol.name}[/]  ({cand.symbol.kind.value})\n"
        f"    Uncertainty: [yellow]{reasons}[/]\n"
    )


def _print_active_card(dc_id: str, cand, repo_root: Path) -> None:
    rel_file = _rel(cand.file, repo_root)
    console.print(
        f"  [{STYLE_ACTIVE}][{dc_id}][/]  {rel_file}:{cand.line}\n"
        f"    [bold]{cand.symbol.name}[/]  ({cand.symbol.kind.value})\n"
        f"    References: {cand.ref_count}\n"
    )


# ---------------------------------------------------------------------------
# SHOW
# ---------------------------------------------------------------------------


@app.command()
def show(
    candidate_id: Annotated[str, typer.Argument(help="Candidate ID, e.g. DC-001")],
    path: Annotated[
        Optional[str],
        typer.Option("--path", help="Repository path (default: current directory)"),
    ] = None,
) -> None:
    """Show the complete evidence for a candidate."""
    repo_root = _resolve_repo(path)

    cand = load_candidate(repo_root, candidate_id)
    if cand is None:
        state_file = state_path(repo_root)
        if not state_file.exists():
            err_console.print(
                f"[red]No scan state found.[/] Run [bold]deadcode scan[/] first."
            )
        else:
            err_console.print(
                f"[red]Candidate {candidate_id} not found.[/] "
                f"Run [bold]deadcode scan[/] to refresh."
            )
        raise typer.Exit(code=1)

    rel_file = _rel(cand.file, repo_root)
    classification = cand.classification.value

    style_map = {
        "PROVABLE": STYLE_PROVABLE,
        "REVIEW": STYLE_REVIEW,
        "ACTIVE": STYLE_ACTIVE,
    }
    cls_style = style_map.get(classification, "white")

    console.print()
    console.rule(f"[bold]{candidate_id}[/]")
    console.print()

    # Core identity
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="dim", width=20)
    table.add_column()

    table.add_row("Symbol", f"[bold]{cand.symbol.name}[/]")
    table.add_row("Qualified name", cand.qualified_name)
    table.add_row("Kind", cand.symbol.kind.value)
    table.add_row("File", rel_file)
    table.add_row("Line", str(cand.line))
    table.add_row("Classification", f"[{cls_style}]{classification}[/]")

    console.print(table)
    console.print()

    # Evidence
    console.print("  [dim]Evidence[/]")
    ev_table = Table(show_header=False, box=None, padding=(0, 2, 0, 2))
    ev_table.add_column(style="dim", width=28)
    ev_table.add_column()

    ev_table.add_row("References", str(cand.ref_count))
    ev_table.add_row("Test references", str(cand.test_ref_count))
    ev_table.add_row(
        "Exported (__all__)",
        "yes" if cand.is_exported else "no" if cand.is_exported is False else "no __all__",
    )
    ev_table.add_row("Is private", "yes" if cand.symbol.is_private else "no")
    ev_table.add_row("Has decorator", "yes" if cand.symbol.has_decorator else "no")

    if cand.import_relationships:
        ev_table.add_row("Import relationships", ", ".join(cand.import_relationships))

    if cand.uncertainty_reasons:
        ev_table.add_row(
            "Uncertainty reasons",
            "[yellow]" + "\n".join(cand.uncertainty_reasons) + "[/]",
        )

    console.print(ev_table)
    console.print()

    # Reference locations
    if cand.ref_locations:
        console.print("  [dim]Reference locations[/]")
        for loc in cand.ref_locations[:10]:
            rel_loc = _rel(loc["file"], repo_root)
            console.print(f"    {rel_loc}:{loc['line']}  [dim]({loc.get('context', '')})[/]")
        if len(cand.ref_locations) > 10:
            console.print(f"    … and {len(cand.ref_locations) - 10} more")
        console.print()

    # Detailed evidence items
    console.print("  [dim]Analysis evidence[/]")
    for ev in cand.evidence:
        icon = ICON_WARN if ev.reason in ("no_references", "provable_unused") else ICON_OK
        console.print(f"    {icon}  [dim]{ev.reason}[/]  {ev.detail}")
    console.print()

    # Proof status
    proof = get_proof(repo_root, candidate_id)
    if proof:
        status = proof.get("status", "?")
        console.print(f"  [dim]Proof status[/]  [{cls_style}]{status}[/]")
        console.print()


# ---------------------------------------------------------------------------
# PROVE
# ---------------------------------------------------------------------------


@app.command()
def prove(
    candidate_id: Annotated[str, typer.Argument(help="Candidate ID, e.g. DC-001")],
    path: Annotated[
        Optional[str],
        typer.Option("--path", help="Repository path (default: current directory)"),
    ] = None,
    timeout: Annotated[
        int,
        typer.Option("--timeout", help="Verification command timeout in seconds"),
    ] = 120,
    no_tests: Annotated[
        bool,
        typer.Option("--no-tests", help="Skip test verification"),
    ] = False,
    no_typecheck: Annotated[
        bool,
        typer.Option("--no-typecheck", help="Skip typecheck verification"),
    ] = False,
) -> None:
    """Prove a candidate is safe to remove using an isolated Git worktree."""
    repo_root = _resolve_repo(path)

    cand = load_candidate(repo_root, candidate_id)
    if cand is None:
        state_file = state_path(repo_root)
        if not state_file.exists():
            err_console.print(
                f"[red]No scan state found.[/] Run [bold]deadcode scan[/] first."
            )
        else:
            err_console.print(
                f"[red]Candidate {candidate_id} not found.[/] "
                f"Run [bold]deadcode scan[/] to refresh."
            )
        raise typer.Exit(code=1)

    rel_file = _rel(cand.file, repo_root)

    console.print()
    console.rule(f"[bold]PROVING {candidate_id}[/]")
    console.print()
    console.print(f"  Candidate  :  [bold]{cand.symbol.name}[/]")
    console.print(f"  Location   :  {rel_file}:{cand.line}")
    console.print(f"  Kind       :  {cand.symbol.kind.value}")
    console.print()

    if cand.classification != SafetyClassification.PROVABLE:
        console.print(
            Panel(
                f"Candidate is classified [bold]{cand.classification.value}[/], not PROVABLE.\n"
                "Only PROVABLE candidates can be proven.\n\n"
                f"Uncertainty reasons: {', '.join(cand.uncertainty_reasons) or 'see deadcode show'}",
                title="[bold yellow]BLOCKED[/]",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=2)

    opts = ProofOptions(
        timeout=timeout,
        run_tests=not no_tests,
        run_typecheck=not no_typecheck,
    )

    # Run proof with live progress
    with console.status("Creating isolated worktree…", spinner="dots"):
        proof = prove_candidate(repo_root, cand, opts)

    # Show step results
    _print_proof_steps(proof)

    # Persist the proof result
    record_proof(repo_root, candidate_id, proof.to_dict())

    console.print()
    if proof.status == ProofStatus.PROVEN:
        console.print(
            Panel(
                f"[bold green]PROVEN SAFE TO REMOVE[/]\n\n"
                f"Run [bold]deadcode apply {candidate_id}[/] to apply the removal.",
                border_style="green",
            )
        )
    elif proof.status == ProofStatus.BLOCKED:
        console.print(
            Panel(
                f"[bold yellow]BLOCKED[/]\n\n{proof.failure_reason}",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=2)
    else:
        console.print(
            Panel(
                f"[bold red]PROOF FAILED[/]\n\n{proof.failure_reason}",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)


def _print_proof_steps(proof) -> None:
    """Render the proof step table to the console."""
    from deadcode.proof_models import RemovalStatus

    steps: list[tuple[str, str, str]] = []  # (label, icon, detail)

    # Worktree
    if proof.worktree_id:
        steps.append(("Creating isolated worktree", ICON_OK, ""))
    elif proof.status == ProofStatus.BLOCKED:
        steps.append(("Creating isolated worktree", ICON_FAIL, proof.failure_reason))
    else:
        steps.append(("Creating isolated worktree", ICON_FAIL, proof.failure_reason))

    # Removal
    rs = proof.removal.status
    if rs == RemovalStatus.SUCCESS:
        steps.append((
            "Removing candidate",
            ICON_OK,
            f"{proof.removal.lines_removed} lines removed",
        ))
    elif rs == RemovalStatus.FAILED:
        steps.append(("Removing candidate", ICON_FAIL, proof.removal.reason))
    else:
        steps.append(("Removing candidate", ICON_SKIP, "not attempted"))

    # Verification steps
    _STATUS_ICON = {
        VerificationStatus.PASSED: ICON_OK,
        VerificationStatus.FAILED: ICON_FAIL,
        VerificationStatus.SKIPPED: ICON_SKIP,
        VerificationStatus.NOT_RUN: ICON_SKIP,
    }
    _STATUS_LABEL = {
        VerificationStatus.PASSED: "",
        VerificationStatus.FAILED: "FAILED",
        VerificationStatus.SKIPPED: "SKIPPED",
        VerificationStatus.NOT_RUN: "not run",
    }
    for v in proof.verifications:
        icon = _STATUS_ICON.get(v.status, ICON_SKIP)
        label = v.name.capitalize()
        detail = ""
        if v.status == VerificationStatus.PASSED:
            if v.name == "pytest" and v.stdout:
                # Extract passed count from pytest output
                for line in v.stdout.splitlines()[-5:]:
                    if "passed" in line:
                        detail = line.strip()
                        break
        elif v.status == VerificationStatus.SKIPPED:
            detail = v.reason
        elif v.status == VerificationStatus.FAILED:
            detail = v.reason or f"exit {v.exit_code}"
        steps.append((label, icon, detail))

    # Re-analysis
    if proof.reanalysis.candidate_absent:
        steps.append(("Re-scanning repository", ICON_OK, "candidate absent"))
    elif proof.reanalysis.error:
        steps.append(("Re-scanning repository", ICON_FAIL, proof.reanalysis.error))
    else:
        steps.append(("Re-scanning repository", ICON_WARN, "candidate still present"))

    # Render
    for label, icon, detail in steps:
        detail_str = f"  [dim]{detail}[/]" if detail else ""
        console.print(f"  {label:<32}{icon}{detail_str}")


# ---------------------------------------------------------------------------
# APPLY
# ---------------------------------------------------------------------------


@app.command()
def apply(
    candidate_id: Annotated[str, typer.Argument(help="Candidate ID, e.g. DC-001")],
    path: Annotated[
        Optional[str],
        typer.Option("--path", help="Repository path (default: current directory)"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Apply a proven candidate removal to the working tree."""
    repo_root = _resolve_repo(path)

    cand = load_candidate(repo_root, candidate_id)
    if cand is None:
        state_file = state_path(repo_root)
        if not state_file.exists():
            err_console.print(
                f"[red]No scan state found.[/] Run [bold]deadcode scan[/] first."
            )
        else:
            err_console.print(
                f"[red]Candidate {candidate_id} not found.[/] "
                f"Run [bold]deadcode scan[/] to refresh."
            )
        raise typer.Exit(code=1)

    rel_file = _rel(cand.file, repo_root)

    # Safety gate: must have a stored PROVEN result
    if not is_proven(repo_root, candidate_id):
        console.print()
        console.print(
            Panel(
                f"[bold yellow]BLOCKED[/]\n\n"
                f"Candidate [bold]{candidate_id}[/] has not been proven safe to remove.\n\n"
                f"Run [bold]deadcode prove {candidate_id}[/] first.",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=2)

    console.print()
    console.rule(f"[bold]APPLY {candidate_id}[/]")
    console.print()
    console.print("  Apply proven removal?\n")
    console.print(f"    [bold]{cand.symbol.name}[/]  ({cand.symbol.kind.value})")
    console.print(f"    {rel_file}:{cand.line}")
    console.print()

    # Confirmation
    if not yes:
        confirmed = typer.confirm("  [y/N]", default=False)
        if not confirmed:
            console.print("\n  [dim]Removal cancelled.[/]\n")
            raise typer.Exit(code=0)

    # Apply the removal directly in the working tree
    try:
        lines_removed = remove_candidate(cand, repo_root)
    except RemovalError as exc:
        console.print(
            Panel(
                f"[bold red]APPLY FAILED[/]\n\n{exc}",
                border_style="red",
            )
        )
        raise typer.Exit(code=1)

    console.print(f"\n  {ICON_OK}  Removed {lines_removed} lines from [cyan]{rel_file}[/]")

    # Re-scan to confirm
    with console.status("Re-scanning to confirm…", spinner="dots"):
        result = analyze_repo(repo_root)

    still_present = any(c.qualified_name == cand.qualified_name for c in result.candidates)
    if still_present:
        console.print(
            f"  {ICON_WARN}  [yellow]Warning:[/] {cand.qualified_name} still appears "
            "in re-analysis (may be a re-export or alias)."
        )
    else:
        console.print(f"  {ICON_OK}  {cand.qualified_name} absent from re-analysis")

    # Update state with new scan
    id_map = assign_ids(result.candidates)
    save_state(
        repo_root,
        id_map,
        files_scanned=result.files_scanned,
        total_definitions=result.total_definitions,
    )

    console.print(
        Panel(
            f"[bold green]APPLIED[/]\n\n"
            f"{cand.symbol.name} removed from {rel_file}\n"
            f"{lines_removed} lines deleted\n\n"
            "The state file has been updated with a fresh scan.\n"
            "[dim]Note: changes are not committed automatically.[/]",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# Version / main entry point
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show version and exit"),
    ] = False,
) -> None:
    """DeadCode — find and safely remove unused Python code."""
    if version:
        console.print("deadcode 0.1.0")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
