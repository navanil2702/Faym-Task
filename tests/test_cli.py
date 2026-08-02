"""Tests for how a run decides where its work list comes from.

The spreadsheet is the specified path and the default: read pending tasks from
Excel, place those returns, write the outcome back. ``--discover`` opts out of
it and reads the account's orders page instead - an extension, never something
a run falls into by default.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil

import pytest

from faym_returns import cli

import dataset


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


@pytest.fixture
def log():
    return logging.getLogger("faym_returns.test")


@pytest.fixture
def my_sheet(tmp_path):
    """A copy of the sample data standing in for somebody's own workbook."""
    path = tmp_path / "my orders.xlsx"
    shutil.copy2(dataset.SOURCE, path)
    return path


# ------------------------------------------------------------------ sheet mode


def test_a_supplied_workbook_is_used_verbatim(my_sheet, log):
    mode = cli._resolve_mode(_args(str(my_sheet)), log)
    assert mode is not None
    assert mode.discover is False
    assert mode.source == my_sheet.resolve()
    assert mode.source != cli.find_sample_workbook()


def test_missing_file_is_reported(tmp_path, log):
    assert cli._resolve_mode(_args(str(tmp_path / "nope.xlsx")), log) is None


# ----------------------------------------------------------------- sample mode


def test_sample_resolves_to_the_bundled_sheet(log):
    mode = cli._resolve_mode(_args("--sample"), log)
    assert mode is not None
    assert mode.sample and not mode.discover
    assert mode.source.name == cli.SAMPLE_NAME


def test_sample_refuses_live(log):
    """Those orders belong to somebody else."""
    assert cli._resolve_mode(_args("--sample", "--live"), log) is None


def test_sample_and_a_path_together_are_ambiguous(my_sheet, log):
    assert cli._resolve_mode(_args(str(my_sheet), "--sample"), log) is None


def test_sample_backdates_far_enough_to_exercise_the_flows():
    """At today's real date every sample row is expired and nothing would run."""
    assert cli.SAMPLE_TODAY < dt.date(2026, 7, 15)


# -------------------------------------------------------------- discovery mode


def test_a_live_run_reads_the_spreadsheet(my_sheet, log):
    """The specified workflow: pending tasks come from Excel, returns get placed."""
    mode = cli._resolve_mode(_args(str(my_sheet), "--live"), log)
    assert mode is not None
    assert mode.discover is False
    assert mode.source == my_sheet.resolve()


def test_no_path_is_never_guessed(log):
    """Not the sample, not discovery - the run stops and asks."""
    assert cli._resolve_mode(_args(), log) is None
    assert cli._resolve_mode(_args("--live"), log) is None


# -------------------------------------------------------------- discovery mode


def test_discovery_is_opt_in(log):
    mode = cli._resolve_mode(_args("--discover", "--platform", "Flipkart"), log)
    assert mode is not None
    assert mode.discover is True
    assert mode.source is None


def test_discovery_can_be_live(log):
    mode = cli._resolve_mode(_args("--discover", "--live", "--platform", "Amazon"), log)
    assert mode is not None
    assert mode.discover is True


def test_discovery_takes_no_workbook(my_sheet, log):
    assert cli._resolve_mode(_args(str(my_sheet), "--discover"), log) is None


def test_discovery_needs_a_platform(log):
    """With no sheet, nothing else says which site the orders live on."""
    assert cli._resolve_mode(_args("--discover"), log) is None


def test_discovery_cannot_run_offline(log):
    assert cli._resolve_mode(_args("--discover", "--offline", "--platform", "Flipkart"), log) is None


def test_discovery_cannot_be_inspected(log):
    """--inspect prints what came out of a sheet, and there is no sheet."""
    assert cli._resolve_mode(_args("--discover", "--inspect", "--platform", "Flipkart"), log) is None


def test_discovery_and_the_sample_are_incompatible(log):
    assert cli._resolve_mode(_args("--discover", "--sample"), log) is None


# ------------------------------------------------------------------ state root


def test_state_root_is_a_real_directory_choice():
    """Profile and screenshots resolve somewhere writable, checkout or not."""
    assert cli._state_root().is_absolute()
