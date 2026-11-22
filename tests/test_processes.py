"""The process list.

Three things were wrong with it at once: the `cpu` column was always 0.0 (and
the list was sorted by it), the figures it did produce were on a different
scale from the system-wide CPU reading shown beside them, and it spent five
seconds fetching a `status` field that nothing displays.
"""
from __future__ import annotations

import pytest

psutil = pytest.importorskip("psutil")

from backend.systemmon import apps


def test_cpu_figures_are_real_rather_than_zero():
    """`cpu_percent` reports usage since the last time you asked, so a single
    pass over fresh handles returns 0.0 for everything -- which is what the
    panel showed, under a column it was sorting by."""
    rows = apps.list_processes(limit=25, sort_by="cpu")
    assert rows, "something must be running"
    assert any(row["cpu"] > 0 for row in rows), (
        "every CPU reading was zero -- the measurement window is missing")


def test_cpu_is_on_the_same_scale_as_the_system_reading():
    """psutil reports percent of one core, so a busy process reads 400% beside
    a system-wide figure of 50%. Divided by the core count they agree."""
    rows = apps.list_processes(limit=25, sort_by="cpu")
    for row in rows:
        assert 0.0 <= row["cpu"] <= 100.0, f"{row['name']} reported {row['cpu']}%"


def test_the_idle_process_is_not_listed_as_a_consumer():
    """On Windows it sits at the top of any CPU sort, and it is the measure of
    the CPU nobody is using."""
    rows = apps.list_processes(limit=40, sort_by="cpu")
    names = {row["name"].lower() for row in rows}
    assert "system idle process" not in names


def test_results_are_sorted_by_the_requested_key():
    by_cpu = apps.list_processes(limit=10, sort_by="cpu")
    assert [r["cpu"] for r in by_cpu] == sorted((r["cpu"] for r in by_cpu), reverse=True)

    by_memory = apps.list_processes(limit=10, sort_by="memory")
    assert [r["memory_mb"] for r in by_memory] == sorted(
        (r["memory_mb"] for r in by_memory), reverse=True)


def test_the_limit_is_respected():
    assert len(apps.list_processes(limit=3)) <= 3
    assert len(apps.list_processes(limit=1)) <= 1


def test_every_row_has_the_fields_the_panel_renders():
    for row in apps.list_processes(limit=5):
        assert set(row) == {"pid", "name", "cpu", "memory_mb"}, (
            "fields that are fetched but never rendered cost real time: "
            f"got {sorted(row)}")


def test_memory_is_reported_for_the_rows_returned():
    rows = apps.list_processes(limit=8, sort_by="cpu")
    assert all("memory_mb" in row for row in rows)
    assert any(row["memory_mb"] > 0 for row in rows)


def test_a_process_that_vanishes_mid_scan_is_skipped_not_fatal():
    """Processes exit while you are enumerating them. That is normal."""
    class _Gone:
        pid = 999999

        def cpu_percent(self, _interval=None):
            raise psutil.NoSuchProcess(999999)

        def name(self):
            raise psutil.NoSuchProcess(999999)

    real_iter = psutil.process_iter
    psutil.process_iter = lambda *a, **k: [_Gone()] + list(real_iter())
    try:
        rows = apps.list_processes(limit=5)
    finally:
        psutil.process_iter = real_iter
    assert isinstance(rows, list)
    assert all(row["pid"] != 999999 for row in rows)
