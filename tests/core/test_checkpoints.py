"""Tests for generated human-facing checkpoint documents."""

from __future__ import annotations

import textwrap
from pathlib import Path

from gpd.core.checkpoints import sync_phase_checkpoints


def _setup_project(tmp_path: Path) -> Path:
    planning = tmp_path / ".gpd" / "phases"
    planning.mkdir(parents=True)
    return tmp_path


def _write_summary(path: Path, *, phase: str, plan: str, one_liner: str, completed: str = "2026-03-17") -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            phase: "{phase}"
            plan: "{plan}"
            completed: "{completed}"
            one-liner: "{one_liner}"
            key-files:
              - src/model.py
            key-decisions:
              - Keep the reduced wedge
            patterns-established:
              - Hidden-damage rejection survives
            ---

            # Summary

            **{one_liner}**

            ## Key Results

            The phase produced a clean checkpoint result.
            """
        ),
        encoding="utf-8",
    )


def _write_milestones_tracker(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            # Milestones

            | Version | Name | Status | Started | Completed | Notes |
            | --- | --- | --- | --- | --- | --- |
            | `v1.0` | Core Framework | open_questions | 2026-03-01 | 2026-03-17 | Boundaries tightened without model inflation |
            """
        ),
        encoding="utf-8",
    )


def _write_milestone_audit(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            ---
            milestone: "v1.0"
            audited: "2026-03-17"
            status: "open_questions"
            open_questions:
              - items:
                  - reserve transfer remains bounded
                  - dynamic P still awaits matched-state breakage
            ---

            # Milestone Audit

            ## Milestone Question

            Can the reduced wedge survive the current pressure tests without inflating state space?

            ## Verdict

            Milestone `v1.0` closes coherently.
            """
        ),
        encoding="utf-8",
    )


def _write_milestone_digest(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """\
            # Research Digest

            ## Narrative Arc

            This milestone converted the current wedge from provisional to disciplined.

            ## Key Results

            | Phase | Result | Equation / Value | Validity Range | Confidence |
            | ----- | ------ | ---------------- | -------------- | ---------- |
            | 01 | Reserve transfer closes in bounded form | `E` stays reserve-like stress headroom | Inclusion-class bounded | Verified |
            | 02 | Dynamic `P` still does not promote | `P(E,T,D)` remains derived | Matched-state closure preserved | Verified |
            """
        ),
        encoding="utf-8",
    )


def test_sync_phase_checkpoints_generates_root_and_phase_docs(tmp_path: Path) -> None:
    cwd = _setup_project(tmp_path)
    phase_dir = cwd / ".gpd" / "phases" / "01-test-phase"
    phase_dir.mkdir()
    _write_summary(phase_dir / "01-SUMMARY.md", phase="01", plan="01", one_liner="Set up the project")
    (phase_dir / "01-VERIFICATION.md").write_text("# Verification\n\nPassed.\n", encoding="utf-8")

    result = sync_phase_checkpoints(cwd)

    assert result.generated is True
    assert result.phase_count == 1
    assert result.milestone_count == 0
    assert "phase-checkpoints/01-test-phase.md" in result.updated_files
    assert "CHECKPOINTS.md" in result.updated_files

    phase_checkpoint = (cwd / "phase-checkpoints" / "01-test-phase.md").read_text(encoding="utf-8")
    assert "# Phase 01 Checkpoint" in phase_checkpoint
    assert "[01-SUMMARY.md](../.gpd/phases/01-test-phase/01-SUMMARY.md)" in phase_checkpoint
    assert "[01-VERIFICATION.md](../.gpd/phases/01-test-phase/01-VERIFICATION.md)" in phase_checkpoint
    assert "The headline result was straightforward" in phase_checkpoint

    checkpoints_index = (cwd / "CHECKPOINTS.md").read_text(encoding="utf-8")
    assert "[Phase 01: Test Phase](phase-checkpoints/01-test-phase.md)" in checkpoints_index

    rerun = sync_phase_checkpoints(cwd)
    assert rerun.updated_files == []
    assert rerun.removed_files == []


def test_sync_phase_checkpoints_uses_latest_summary_and_keeps_earlier_notes(tmp_path: Path) -> None:
    cwd = _setup_project(tmp_path)
    phase_dir = cwd / ".gpd" / "phases" / "02-two-step-phase"
    phase_dir.mkdir()
    _write_summary(phase_dir / "01-SUMMARY.md", phase="02", plan="01", one_liner="Built the first draft")
    _write_summary(phase_dir / "02-SUMMARY.md", phase="02", plan="02", one_liner="Refined the final result")

    sync_phase_checkpoints(cwd)

    phase_checkpoint = (cwd / "phase-checkpoints" / "02-two-step-phase.md").read_text(encoding="utf-8")
    assert "Refined the final result" in phase_checkpoint
    assert "## Earlier Plan Notes" in phase_checkpoint
    assert "Built the first draft." in phase_checkpoint


def test_sync_phase_checkpoints_removes_stale_checkpoint_docs(tmp_path: Path) -> None:
    cwd = _setup_project(tmp_path)
    phase_dir = cwd / ".gpd" / "phases" / "03-real-phase"
    phase_dir.mkdir()
    _write_summary(phase_dir / "01-SUMMARY.md", phase="03", plan="01", one_liner="Closed the phase")

    checkpoint_dir = cwd / "phase-checkpoints"
    checkpoint_dir.mkdir()
    stale = checkpoint_dir / "99-old-phase.md"
    stale.write_text("# stale\n", encoding="utf-8")

    result = sync_phase_checkpoints(cwd)

    assert "phase-checkpoints/99-old-phase.md" in result.removed_files
    assert stale.exists() is False


def test_sync_phase_checkpoints_generates_milestone_docs_and_updates_root_index(tmp_path: Path) -> None:
    cwd = _setup_project(tmp_path)
    planning = cwd / ".gpd"
    milestones_dir = planning / "milestones"
    milestones_dir.mkdir(parents=True)
    _write_milestones_tracker(planning / "MILESTONES.md")
    _write_milestone_audit(milestones_dir / "v1.0-MILESTONE-AUDIT.md")
    _write_milestone_digest(milestones_dir / "v1.0" / "RESEARCH-DIGEST.md")
    (milestones_dir / "v1.0-ROADMAP.md").write_text("# Archived roadmap\n", encoding="utf-8")
    (milestones_dir / "v1.0-REQUIREMENTS.md").write_text("# Archived requirements\n", encoding="utf-8")

    result = sync_phase_checkpoints(cwd)

    assert result.milestone_count == 1
    assert result.milestone_checkpoint_dir == "milestone-checkpoints"
    assert "milestone-checkpoints/v1.0.md" in result.updated_files

    milestone_checkpoint = (cwd / "milestone-checkpoints" / "v1.0.md").read_text(encoding="utf-8")
    assert "# Milestone v1.0 Checkpoint" in milestone_checkpoint
    assert "[v1.0-MILESTONE-AUDIT.md](../.gpd/milestones/v1.0-MILESTONE-AUDIT.md)" in milestone_checkpoint
    assert "[RESEARCH-DIGEST.md](../.gpd/milestones/v1.0/RESEARCH-DIGEST.md)" in milestone_checkpoint
    assert "The headline result was straightforward" in milestone_checkpoint

    checkpoints_index = (cwd / "CHECKPOINTS.md").read_text(encoding="utf-8")
    assert "## Milestone Index" in checkpoints_index
    assert "[Milestone v1.0: Core Framework](milestone-checkpoints/v1.0.md)" in checkpoints_index


def test_sync_phase_checkpoints_removes_stale_milestone_docs(tmp_path: Path) -> None:
    cwd = _setup_project(tmp_path)
    milestone_checkpoint_dir = cwd / "milestone-checkpoints"
    milestone_checkpoint_dir.mkdir()
    stale = milestone_checkpoint_dir / "v9.9.md"
    stale.write_text("# stale milestone\n", encoding="utf-8")

    result = sync_phase_checkpoints(cwd)

    assert "milestone-checkpoints/v9.9.md" in result.removed_files
    assert stale.exists() is False
