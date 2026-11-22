"""Working unattended.

Two things are tested here. The guard, because with every permission now
granted it is the only thing standing between an unsupervised agent and the
rest of the machine. And the clocks, because the entire promise -- "come
home to it wrapping up or finished" -- is arithmetic that either works or
leaves someone walking in on a half-finished job.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.autonomy.guard import (ALWAYS_SAFE, NEVER_UNATTENDED, Guard,
                                    Verdict)
from backend.autonomy.session import MAX_WORK_MINUTES, AutonomySession


@pytest.fixture
def guard(tmp_path):
    return Guard(tmp_path / "workspace")


# ===========================================================================
# the guard
# ===========================================================================
def test_reading_is_allowed_anywhere_within_reach(guard, tmp_path):
    decision = guard.check("read_file", {"path": str(tmp_path / "notes.txt")})
    assert decision.verdict is Verdict.ALLOW


def test_writing_inside_the_workspace_is_allowed(guard):
    decision = guard.check("write_file",
                           {"path": str(guard.workspace / "app" / "index.html")})
    assert decision.verdict is Verdict.ALLOW
    assert "workspace" in decision.reason


def test_writing_outside_the_workspace_is_held_not_refused(tmp_path, guard):
    """Held, not refused: it may well be the right thing to do, but it is
    not a call to make while nobody is watching."""
    decision = guard.check("write_file", {"path": str(tmp_path / "elsewhere.txt")})
    assert decision.verdict is Verdict.HOLD
    assert "outside" in decision.reason


def test_deleting_outside_the_workspace_is_held(tmp_path, guard):
    decision = guard.check("delete_file", {"path": str(tmp_path / "important.doc")})
    assert decision.verdict is Verdict.HOLD
    assert "Deleting" in decision.reason


def test_deleting_inside_the_workspace_is_allowed(guard):
    """The point of giving it a workspace is that it can work in it."""
    decision = guard.check("delete_file",
                           {"path": str(guard.workspace / "scratch.tmp")})
    assert decision.verdict is Verdict.ALLOW


@pytest.mark.parametrize("path", [
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "C:\\Program Files\\something\\app.exe",
    "/etc/passwd",
    "/usr/bin/python",
    "C:\\Users\\Mike\\.ssh\\id_rsa",
])
def test_system_and_credential_paths_are_refused_for_writes(guard, path):
    for tool in ("write_file", "delete_file"):
        decision = guard.check(tool, {"path": path})
        assert decision.verdict is Verdict.REFUSE, (tool, path)


def test_a_secret_file_is_held_even_for_reading(guard, tmp_path):
    """An unattended run has no reason to need a .env and every reason not
    to be copying one around."""
    decision = guard.check("read_file", {"path": str(tmp_path / ".env")})
    assert decision.verdict is Verdict.HOLD
    assert "secret" in decision.reason.lower()


@pytest.mark.parametrize("tool", sorted(NEVER_UNATTENDED))
def test_the_never_list_is_refused_whatever_the_arguments(guard, tool):
    decision = guard.check(tool, {"path": str(guard.workspace / "x")})
    assert decision.verdict is Verdict.REFUSE


def test_self_editing_is_never_done_unattended():
    """The one failure that can stop it being stoppable."""
    assert any("selfedit" in tool for tool in NEVER_UNATTENDED)


def test_shell_is_held_unless_turned_on_for_the_session(tmp_path):
    closed = Guard(tmp_path / "w")
    assert closed.check("run_shell_command", {}).verdict is Verdict.HOLD

    opened = Guard(tmp_path / "w", allow_shell=True)
    assert opened.check("run_shell_command", {}).verdict is Verdict.ALLOW


def test_high_risk_actions_without_a_path_are_held(guard):
    decision = guard.check("something_drastic", {}, risk="critical")
    assert decision.verdict is Verdict.HOLD


def test_ordinary_actions_proceed(guard):
    assert guard.check("list_processes", {}).verdict is Verdict.ALLOW
    assert guard.check("summarise", {}, risk="low").verdict is Verdict.ALLOW


def test_the_safe_list_and_the_never_list_do_not_overlap():
    assert not (ALWAYS_SAFE & NEVER_UNATTENDED)


# ===========================================================================
# the clocks
# ===========================================================================
def test_working_longer_than_you_are_away_is_clamped(tmp_path):
    """Otherwise you walk in on a session still mid-job, which is the one
    thing the wrap-up exists to prevent."""
    session = AutonomySession("x", away_minutes=60, work_minutes=600,
                              workspace=tmp_path)
    assert session.work_minutes == 60


def test_a_day_is_the_ceiling(tmp_path):
    session = AutonomySession("x", away_minutes=10_000, work_minutes=10_000,
                              workspace=tmp_path)
    assert session.work_minutes == MAX_WORK_MINUTES


def test_the_wrap_up_reserve_scales_but_has_a_floor(tmp_path):
    short = AutonomySession("x", 30, 30, tmp_path)
    long_run = AutonomySession("x", 600, 600, tmp_path)

    # There is a floor, so a tiny run still gets a moment to finish up...
    assert short.wrap_minutes >= 2.0
    assert long_run.wrap_minutes > short.wrap_minutes
    # ...and a ceiling, so a long run does not spend an hour tidying.
    assert long_run.wrap_minutes <= 45.0


def test_the_reserve_does_not_swallow_a_short_run(tmp_path):
    """The floor used to be eight minutes, which collided with the
    half-budget cap: a twelve-minute run reserved six and stopped starting
    work at six, having been asked for twelve."""
    for budget in (10, 12, 20, 30):
        session = AutonomySession("x", budget, budget, tmp_path)
        working = budget - session.wrap_minutes
        assert working >= budget * 0.7, (
            f"a {budget}-minute run only works for {working:.1f} minutes")


def test_the_reserve_never_eats_the_whole_budget(tmp_path):
    tiny = AutonomySession("x", 5, 5, tmp_path)
    assert tiny.wrap_minutes <= tiny.work_minutes / 2


def test_a_fresh_session_is_not_yet_wrapping_up(tmp_path):
    session = AutonomySession("x", 240, 180, tmp_path)
    session.started_at = datetime.now(timezone.utc)
    assert not session.should_wrap_up
    assert not session.expired


def test_a_session_near_its_end_wants_to_wrap_up(tmp_path):
    session = AutonomySession("x", 120, 120, tmp_path)
    session.started_at = (datetime.now(timezone.utc)
                          - timedelta(minutes=120 - session.wrap_minutes + 1))
    assert session.should_wrap_up
    assert not session.expired


# ===========================================================================
# the backlog and the journal
# ===========================================================================
def test_jobs_are_added_without_duplicates(tmp_path):
    session = AutonomySession("x", 240, 180, tmp_path)
    assert session.add_jobs(["build the page", "write the tests"]) == 2
    assert session.add_jobs(["Build The Page"]) == 0
    assert len(session.jobs) == 2


def test_an_empty_backlog_is_reported(tmp_path):
    session = AutonomySession("x", 240, 180, tmp_path)
    assert session.backlog_empty
    session.add_jobs(["something"])
    assert not session.backlog_empty
    session.finish_job(session.next_job, "done")
    assert session.backlog_empty


def test_held_actions_keep_what_was_wanted_not_just_the_rule(tmp_path):
    """"Held a delete" is useless a day later."""
    session = AutonomySession("x", 240, 180, tmp_path)
    decision = session.guard.check("delete_file",
                                   {"path": "C:/Users/Mike/taxes.pdf"})
    session.hold(decision, "delete the old tax return")

    assert len(session.held) == 1
    assert session.held[0]["what"] == "delete the old tax return"
    assert session.held[0]["reason"]
    assert session.held[0]["detail"]["path"].endswith("taxes.pdf")


def test_the_report_leads_with_what_matters(tmp_path):
    session = AutonomySession("x", 240, 180, tmp_path)
    session.started_at = datetime.now(timezone.utc)

    session.add_jobs(["build it", "test it", "document it"])
    session.finish_job(session.jobs[0], "built")
    session.finish_job(session.jobs[1], "flaky", done=False)
    session.hold(session.guard.check("delete_file", {"path": "/tmp/x"}),
                 "remove the old build")

    report = session.write_report()
    assert "Finished (1)" in report
    assert "Held for you to decide (1)" in report
    assert "Abandoned (1)" in report
    assert "Not reached (1)" in report
    # What was done and what needs a decision come before the rest.
    assert report.index("Finished") < report.index("Abandoned")
    assert report.index("Held for you") < report.index("Not reached")


def test_a_run_with_nothing_finished_says_so(tmp_path):
    session = AutonomySession("x", 240, 180, tmp_path)
    session.started_at = datetime.now(timezone.utc)
    assert "Nothing was finished." in session.write_report()


def test_stopping_is_requested_not_forced(tmp_path):
    session = AutonomySession("x", 240, 180, tmp_path)
    assert not session.stopping
    session.request_stop()
    assert session.stopping
    assert any(entry.message == "stop requested" for entry in session.journal)


# ===========================================================================
# "done" has to mean something happened
# ---------------------------------------------------------------------------
# The first real run marked twenty-eight jobs finished in two minutes. Most
# of those replies were "it seems the file already exists, would you like to
# overwrite it?" -- a question, asked of nobody, that changed nothing. The
# runner counted any returned text as success.
# ===========================================================================
def test_a_question_is_not_a_report_of_work():
    from backend.autonomy.runner import _asked_a_question

    for reply in ("It seems the file already exists. Would you like to overwrite it?",
                  "Shall I create it instead?",
                  "I don't know how to do that without network access.",
                  "Let me know which you'd prefer."):
        assert _asked_a_question(reply), reply


def test_a_report_of_work_is_not_mistaken_for_a_question():
    from backend.autonomy.runner import _asked_a_question

    for reply in ("Created clock.html with a live digital clock and the date.",
                  "Wrote the file and verified it opens in a browser.",
                  "Added three tests; all pass."):
        assert not _asked_a_question(reply), reply


def test_the_workspace_snapshot_notices_real_changes(tmp_path):
    from backend.autonomy.runner import _snapshot

    before = _snapshot(tmp_path)
    assert before == set()

    (tmp_path / "clock.html").write_text("<html></html>", encoding="utf-8")
    after = _snapshot(tmp_path)
    assert after != before

    # An edit in place counts too, not just a new file.
    (tmp_path / "clock.html").write_text("<html>much longer</html>", encoding="utf-8")
    assert _snapshot(tmp_path) != after


def test_a_missing_workspace_snapshots_as_empty(tmp_path):
    from backend.autonomy.runner import _snapshot
    assert _snapshot(tmp_path / "not-there") == set()


def test_research_is_rationed():
    """It fired six times in ninety seconds and invented twenty-four jobs
    for a request that was one HTML page."""
    from backend.autonomy.runner import (RESEARCH_MAX_PASSES,
                                         RESEARCH_MIN_GAP_MINUTES)

    assert RESEARCH_MAX_PASSES <= 5
    assert RESEARCH_MIN_GAP_MINUTES >= 5.0


def test_job_titles_survive_a_fenced_json_reply():
    """Small local models wrap JSON in prose and fences however firmly they
    are told not to."""
    from backend.autonomy.runner import _parse_list

    assert _parse_list('```json\n["build it", "test it"]\n```') == ["build it", "test it"]
    assert _parse_list('Sure! Here you go:\n["one", "two"]\nHope that helps.') \
        == ["one", "two"]


def test_a_plain_list_reply_still_parses():
    from backend.autonomy.runner import _parse_list

    parsed = _parse_list("1. Create the file\n2. Write the markup\n3. Test it")
    assert parsed == ["Create the file", "Write the markup", "Test it"]


def test_an_empty_reply_yields_no_jobs():
    from backend.autonomy.runner import _parse_list
    assert _parse_list("") == []
    assert _parse_list("   ") == []


def test_held_actions_tell_the_model_not_to_retry():
    """An agent whose call silently does nothing will try it again until the
    round limit. One told why will work around it."""
    import inspect

    from backend.autonomy.runner import GuardedRegistry

    source = inspect.getsource(GuardedRegistry.dispatch)
    assert "do not retry" in source.lower()


# ===========================================================================
# an empty backlog is not a finished job
# ---------------------------------------------------------------------------
# A ten-minute run finished its plan in two minutes and shut down, having
# been asked for ten. Rationing research stopped it inventing filler, but
# "no research right now" was being treated as "no work left" -- so the
# whole point of leaving it working was lost.
# ===========================================================================
def test_the_loop_does_not_exit_on_an_empty_backlog():
    import inspect

    from backend.autonomy.runner import Runner

    # The task loop. Building one thing has its own loop, with its own
    # reasons for stopping, tested in test_autonomy_builder.
    source = inspect.getsource(Runner._work)
    code = "\n".join(line for line in source.splitlines()
                     if not line.strip().startswith("#"))

    # The only way out of the loop is time, or being asked to stop.
    assert "_improve()" in code, "it should polish what exists when research is due"
    assert "IDLE_WAIT_SECONDS" in code, "it should wait rather than give up"
    assert 'session.note("note", "nothing further to do")' not in code


def test_improvement_rounds_are_capped():
    """Polishing forever is its own kind of filler."""
    from backend.autonomy.runner import MAX_IMPROVE_PASSES

    assert 1 <= MAX_IMPROVE_PASSES <= 10


def test_the_idle_wait_is_short_enough_to_resume_promptly():
    from backend.autonomy.runner import IDLE_WAIT_SECONDS

    assert 5 <= IDLE_WAIT_SECONDS <= 120


# ===========================================================================
# saying you are back
# ===========================================================================
@pytest.mark.asyncio
async def test_saying_you_are_back_hands_over_and_clears_the_slot(tmp_path):
    """A finished run used to sit as `current` for ever, so the panel kept
    showing yesterday's report and refused to start anything new."""
    from backend.autonomy.service import AutonomyService
    from backend.autonomy.session import Phase

    class _Settings:
        DATA_DIR = tmp_path

    service = AutonomyService(_Settings())
    session = AutonomySession("build a thing", 60, 60, tmp_path / "w")
    session.started_at = datetime.now(timezone.utc)
    session.add_jobs(["one", "two"])
    session.finish_job(session.jobs[0], "done")
    session.phase = Phase.DONE
    service.current = session

    reply = await service.welcome_back()
    assert reply["acknowledged"] is True
    assert reply["jobs_done"] == 1
    assert reply["report"]
    assert service.current is None, "the slot must be free for the next run"


@pytest.mark.asyncio
async def test_saying_you_are_back_early_asks_it_to_wrap_up(tmp_path):
    """Coming home early should not throw away work in flight."""
    from backend.autonomy.service import AutonomyService
    from backend.autonomy.session import Phase

    class _Settings:
        DATA_DIR = tmp_path

    service = AutonomyService(_Settings())
    session = AutonomySession("build a thing", 60, 60, tmp_path / "w")
    session.started_at = datetime.now(timezone.utc)
    session.phase = Phase.WORKING
    service.current = session

    reply = await service.welcome_back()
    assert reply["acknowledged"] is False
    assert session.stopping, "it should have been asked to wrap up"
    assert service.current is session, "and not discarded mid-job"


@pytest.mark.asyncio
async def test_saying_you_are_back_with_nothing_running_is_harmless(tmp_path):
    from backend.autonomy.service import AutonomyService

    class _Settings:
        DATA_DIR = tmp_path

    reply = await AutonomyService(_Settings()).welcome_back()
    assert reply["acknowledged"] is False
    assert "Nothing" in reply["detail"]


# ===========================================================================
# handing the work over
# ---------------------------------------------------------------------------
# "It's in a folder somewhere" is not delivery. A run has to end in
# something that can be opened, saved or sent.
# ===========================================================================
@pytest.fixture
def service_with_output(tmp_path):
    from backend.autonomy.service import AutonomyService

    class _Settings:
        DATA_DIR = tmp_path

    service = AutonomyService(_Settings())
    workspace = service.root / "20261114-120000"
    (workspace / "assets").mkdir(parents=True)
    (workspace / "clock.html").write_text("<html>clock</html>", encoding="utf-8")
    (workspace / "assets" / "style.css").write_text("body{}", encoding="utf-8")
    (workspace / "WHAT-I-DID.md").write_text("# notes", encoding="utf-8")
    return service


def test_everything_produced_is_listed(service_with_output):
    made = service_with_output.produced()
    names = {f["name"] for f in made["files"]}
    assert names == {"clock.html", "style.css", "WHAT-I-DID.md"}
    assert all(f["bytes"] > 0 for f in made["files"])


def test_only_pages_are_marked_openable(service_with_output):
    """Offering "open" on a .css would be a lie about what happens."""
    made = service_with_output.produced()
    openable = {f["name"] for f in made["files"] if f["openable"]}
    assert openable == {"clock.html"}


def test_nested_files_keep_their_path(service_with_output):
    made = service_with_output.produced()
    paths = {f["path"] for f in made["files"]}
    assert "assets/style.css" in paths


def test_a_produced_file_resolves(service_with_output):
    resolved = service_with_output.resolve_file("clock.html")
    assert resolved is not None and resolved.read_text(encoding="utf-8")


@pytest.mark.parametrize("attempt", [
    "../../../../etc/passwd",
    "..\..\..\Windows\System32\drivers\etc\hosts",
    "../../.env",
    "/etc/passwd",
])
def test_paths_cannot_escape_the_workspace(service_with_output, attempt):
    """The path comes from a client, and joining client text onto a
    directory is how traversal happens."""
    assert service_with_output.resolve_file(attempt) is None


def test_a_missing_file_resolves_to_nothing(service_with_output):
    assert service_with_output.resolve_file("not-there.html") is None


def test_a_stamp_cannot_escape_the_autonomy_root(service_with_output):
    assert service_with_output.resolve_file("clock.html", stamp="../..") is None


def test_the_whole_run_zips_up(service_with_output):
    import zipfile

    bundle = service_with_output.archive()
    assert bundle is not None and bundle.exists()
    with zipfile.ZipFile(bundle) as archive:
        names = set(archive.namelist())
    assert "clock.html" in names
    assert "assets/style.css" in names


def test_the_archive_is_rebuilt_rather_than_cached(service_with_output):
    """A run still working will have more in it a minute from now, and a
    stale zip is worse than a slow one."""
    import zipfile

    first = service_with_output.archive()
    (service_with_output.root / "20261114-120000" / "later.txt").write_text(
        "added after the first zip", encoding="utf-8")
    second = service_with_output.archive()

    with zipfile.ZipFile(second) as archive:
        assert "later.txt" in archive.namelist()
    assert first == second


def test_runs_survive_the_process_that_made_them(service_with_output):
    """The live session is lost on restart; the files are not."""
    runs = service_with_output.runs()
    assert len(runs) == 1
    assert runs[0]["stamp"] == "20261114-120000"
    assert runs[0]["files"] == 3


def test_an_empty_workshop_lists_nothing(tmp_path):
    from backend.autonomy.service import AutonomyService

    class _Settings:
        DATA_DIR = tmp_path

    service = AutonomyService(_Settings())
    assert service.runs() == []
    assert service.produced()["files"] == []
