"""Tests for backend edit jobs (services/backend_edit_jobs).

Runnable with plain stdlib:  python -m unittest discover -s panel/backend/tests

Задание: правка профилей одним коммитом, затем раскатка каждого изменённого
профиля ровно один раз; сбой одного профиля не останавливает остальные.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.backend_address import (  # noqa: E402
    Action,
    AddressEdit,
    BatchPlan,
    Outcome,
    ProfileChange,
    ProfileRef,
    RuleOutcome,
    SkipReason,
)
from app.services.backend_edit_jobs import JOB_TTL_SEC, EditJobManager, JobStage  # noqa: E402

EDIT = AddressEdit(ip="62.50.146.225", port=8443, all_ports=True, action=Action.DELETE)
REFS = [ProfileRef("haproxy", 1, "NL relays"), ProfileRef("dnat", 2, "VK")]
PLAN = BatchPlan(
    items=[[ProfileChange("haproxy", 1, "NL relays", 5, [
        RuleOutcome("lb", Outcome.CHANGED),
        RuleOutcome("vless-nl", Outcome.SKIPPED, SkipReason.LAST_BACKEND),
    ])]],
    changed=REFS,
)


class FakeSession:
    def __init__(self):
        self.commit = mock.AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class JobTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, planner=None, syncer=None, clock=None):
        self.session = FakeSession()
        return EditJobManager(
            session_factory=lambda: self.session,
            planner=planner or mock.AsyncMock(return_value=PLAN),
            syncer=syncer or mock.AsyncMock(),
            clock=clock or (lambda: 1000.0),
        )

    async def test_edit_then_rollout_each_profile_once(self):
        syncer = mock.AsyncMock()
        manager = self.manager(syncer=syncer)
        job = manager.create([EDIT])
        self.assertEqual(job.stage, JobStage.EDITING)
        await manager.run(job)
        self.session.commit.assert_awaited_once()
        self.assertEqual([call.args[0] for call in syncer.await_args_list], REFS)
        self.assertEqual((job.stage, job.current, job.items), (JobStage.DONE, None, [{"changed": 1, "skipped": 1}]))
        self.assertEqual(job.finished_at, 1000.0)

    async def test_one_failed_rollout_does_not_stop_others(self):
        syncer = mock.AsyncMock(side_effect=[RuntimeError("node api down"), None])
        manager = self.manager(syncer=syncer)
        job = manager.create([EDIT])
        await manager.run(job)
        self.assertEqual((job.stage, job.failures, syncer.await_count), (JobStage.DONE, ["NL relays"], 2))

    async def test_failed_edit_is_reported(self):
        manager = self.manager(planner=mock.AsyncMock(side_effect=RuntimeError("db gone")))
        job = manager.create([EDIT])
        await manager.run(job)
        self.assertEqual((job.stage, job.error), (JobStage.FAILED, "db gone"))

    async def test_finished_jobs_expire_running_stay(self):
        now = [1000.0]
        manager = self.manager(clock=lambda: now[0])
        done = manager.create([EDIT])
        await manager.run(done)
        running = manager.create([EDIT])
        now[0] += JOB_TTL_SEC + 1
        self.assertEqual([job.id for job in manager.recent()], [running.id])

    def test_job_dict_for_the_strip(self):
        job = self.manager().create([EDIT])
        data = job.to_dict(progress=[])
        self.assertEqual((data["stage"], data["edits"][0]["action"], data["profiles"]), ("editing", "delete", []))


if __name__ == "__main__":
    unittest.main()
