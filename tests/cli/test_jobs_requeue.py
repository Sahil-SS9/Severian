"""0.6.3: ``severian jobs requeue`` — put failed (or dead-lettered) jobs back, dry run by default.

On 21/11 the 451 user-fact jobs that a model outage had failed for good were reset by hand with
SQL, because ``requeue_job`` takes only dead-letter rows. This command replaces that: it picks jobs
by kind and, with ``--transient``, only those whose error says the model server was down, busy or
slow (never an unreadable reply, which would fail the same way again). Without ``--confirm`` it
prints the plan and changes nothing. With it, each job returns to ``pending`` with a fresh budget
and one ``job.requeue`` audit row (ids and counts, never the error text or the turn).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from severian.application.jobrecovery import is_transient_error, plan_requeue
from severian.cli.app import cli
from severian.composition import build_bundle
from severian.domain.models import Collection, Scope

SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="shared")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# The live store's failure reasons on 21/11, in the extractor's own wording (counts in comments).
REFUSED = "<urlopen error [Errno 111] Connection refused>"  # 280
NO_BACKEND = "HTTP Error 503: Service Unavailable (reply: '{\"error\": \"no_backend\"}')"  # 122
TIMED_OUT = "timed out"  # 17
LOADING = "HTTP Error 503: Service Unavailable (reply: '{\"error\":{\"message\":\"Loading model\"}}')"  # 4
BUSY = "HTTP Error 409: Conflict (reply: '{\"error\": \"request_in_progress\"}')"  # 2
UNREADABLE = "no JSON object or list in the reply (reply: 'Connection refused, said the user')"
CUT_OFF = "reply cut off at max_tokens (reply: '{\"1\": [\"The user')"


def _plain(text: str) -> str:
    return " ".join(_ANSI.sub("", text).replace("│", " ").split())


RESET = "[Errno 104] Connection reset by peer"


@pytest.mark.parametrize("error", [REFUSED, NO_BACKEND, TIMED_OUT, LOADING, BUSY, RESET])
def test_server_faults_are_transient(error):
    assert is_transient_error(error)


@pytest.mark.parametrize("error", [UNREADABLE, CUT_OFF, None, "", "fact extraction raised KeyError: 'x'"])
def test_reply_problems_are_not_transient_even_when_the_reply_mentions_a_fault(error):
    assert not is_transient_error(error)


def _store(tmp_path: Path):
    bundle = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    try:
        bundle.repository.create_collection(Collection(id="profile", tenant_id="kensei", name="Profile"))
    except Exception:  # noqa: BLE001 - already there from an earlier open
        pass
    return bundle


def _fail(bundle, observation_id: str, error: str, kind: str = "extract_facts") -> str:
    job = bundle.repository.enqueue_job(SCOPE, kind, {"observation_id": observation_id})
    bundle.repository.claim_job("w1", kind=kind)
    bundle.repository.complete_job(job.id, "failed", worker_id="w1", error=error)
    return str(job.id)


def _seed(tmp_path: Path) -> dict[str, str]:
    bundle = _store(tmp_path)
    ids = {
        "refused": _fail(bundle, "a", REFUSED),
        "no_backend": _fail(bundle, "b", NO_BACKEND),
        "unreadable": _fail(bundle, "c", UNREADABLE),
        "derive": _fail(bundle, "d", TIMED_OUT, kind="derive"),
    }
    bundle.close()
    return ids


def _flags(folder: Path) -> list[str]:
    return ["--database", str(folder / "s.db"), "--fts", str(folder / "s.fts"), "--vectors", str(folder / "s.vec")]


def _statuses(tmp_path: Path) -> dict[str, tuple[str, int]]:
    bundle = _store(tmp_path)
    try:
        return {
            job.id: (job.status, job.attempts)
            for status in ("pending", "failed", "dead_letter")
            for job in bundle.repository.list_jobs(status=status)
        }
    finally:
        bundle.close()


def test_the_plan_picks_transient_failures_of_one_kind(tmp_path):
    ids = _seed(tmp_path)
    bundle = _store(tmp_path)
    plan = plan_requeue(bundle.repository, kind="extract_facts", transient_only=True)
    assert sorted(plan.job_ids) == sorted([ids["refused"], ids["no_backend"]])
    assert (plan.matched, plan.skipped_other_errors) == (2, 1)
    everything = plan_requeue(bundle.repository, kind="extract_facts", transient_only=False)
    assert sorted(everything.job_ids) == sorted([ids["refused"], ids["no_backend"], ids["unreadable"]])
    bundle.close()


def test_the_command_is_a_dry_run_by_default(tmp_path):
    ids = _seed(tmp_path)
    before = _statuses(tmp_path)
    result = CliRunner().invoke(cli, ["jobs", "requeue", *_flags(tmp_path), "--kind", "extract_facts", "--transient"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["confirmed"] is False and payload["requeued"] == 0 and payload["matched"] == 2
    assert sorted(payload["job_ids"]) == sorted([ids["refused"], ids["no_backend"]])
    assert "Connection refused" not in result.output and "user" not in json.dumps(payload["job_ids"])
    assert _statuses(tmp_path) == before  # nothing changed


def test_confirm_requeues_only_the_planned_jobs_and_audits_them(tmp_path):
    ids = _seed(tmp_path)
    result = CliRunner().invoke(
        cli, ["jobs", "requeue", *_flags(tmp_path), "--kind", "extract_facts", "--transient", "--confirm"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["confirmed"] is True and payload["requeued"] == 2
    after = _statuses(tmp_path)
    assert after[ids["refused"]] == after[ids["no_backend"]] == ("pending", 0)
    assert after[ids["unreadable"]] == ("failed", 1) and after[ids["derive"]] == ("failed", 1)
    bundle = _store(tmp_path)
    audit = [event for event in bundle.repository.list_audit(SCOPE) if event.action == "job.requeue"]
    bundle.close()
    assert sorted(event.target_id for event in audit) == sorted([ids["refused"], ids["no_backend"]])
    assert all("refused" not in json.dumps(event.metadata).lower() for event in audit)
    again = CliRunner().invoke(
        cli, ["jobs", "requeue", *_flags(tmp_path), "--kind", "extract_facts", "--transient", "--confirm"]
    )
    assert json.loads(again.output)["requeued"] == 0  # idempotent


def test_dead_lettered_jobs_can_be_requeued_in_bulk(tmp_path):
    bundle = _store(tmp_path)
    job = bundle.repository.enqueue_job(SCOPE, "extract_facts", {"observation_id": "a"})
    bundle.repository.claim_job("w1", kind="extract_facts")
    bundle.repository.complete_job(job.id, "dead_letter", worker_id="w1", error=REFUSED)
    bundle.close()
    flags = ["jobs", "requeue", *_flags(tmp_path), "--kind", "extract_facts", "--status", "dead_letter", "--confirm"]
    result = CliRunner().invoke(cli, flags)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["requeued"] == 1
    assert _statuses(tmp_path)[job.id] == ("pending", 0)


def test_the_command_refuses_other_states_and_needs_a_kind(tmp_path):
    _seed(tmp_path)
    bad = CliRunner().invoke(cli, ["jobs", "requeue", *_flags(tmp_path), "--kind", "extract_facts", "--status", "done"])
    assert bad.exit_code != 0 and "--status" in _plain(bad.output)
    missing = CliRunner().invoke(cli, ["jobs", "requeue", *_flags(tmp_path)])
    assert missing.exit_code != 0 and "--kind" in _plain(missing.output)
