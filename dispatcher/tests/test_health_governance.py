import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.models import Agent, AgentRehydrationJob
from app.services import heartbeat


class _ExecResult:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def scalars(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _HeartbeatSession:
    def __init__(self, project, predecessor, default_team):
        self._project = project
        self._predecessor = predecessor
        self._default_team = default_team
        self.added = []
        self.execute_calls = 0

    async def get(self, model, key, options=None):  # noqa: ARG002
        name = getattr(model, "__name__", "")
        if name == "Project":
            return self._project
        return None

    async def execute(self, query):  # noqa: ARG002
        self.execute_calls += 1
        if self.execute_calls == 1:
            return _ExecResult(rows=[self._predecessor])
        return _ExecResult(one=self._default_team)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


class HealthGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_registration_creates_successor_rehydration_job(self):
        project = SimpleNamespace(
            id="proj1234",
            role_model_map={"mid": "deepseek-chat"},
            config={"same_role_relation_mode": "auto", "enable_successor_inherit": True},
        )
        predecessor = SimpleNamespace(
            id="mid-prev",
            last_heartbeat_status="offline",
            current_task_id=None,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            config={
                "context_versions": {"k1": "v1"},
                "retriever_ready": True,
                "local_checkpoint": {"x": 1},
            },
        )
        default_team = SimpleNamespace(id="team-default")
        session = _HeartbeatSession(project, predecessor, default_team)

        agent = await heartbeat._register_agent_from_heartbeat(
            session,
            agent_id="mid-proj1234-worker2",
            project_id="proj1234",
            role="mid",
            supervisor_id="architect-proj1234",
            status="idle",
        )

        self.assertIsNotNone(agent)
        self.assertIsInstance(agent, Agent)
        self.assertEqual(agent.team_id, "team-default")
        self.assertEqual(agent.config["role_relationship"]["predecessor_agent_id"], "mid-prev")
        self.assertTrue(agent.config["role_relationship"]["is_successor"])
        self.assertEqual(agent.config["successor_inherit"]["from_agent_id"], "mid-prev")

        rehydrate_jobs = [x for x in session.added if isinstance(x, AgentRehydrationJob)]
        self.assertEqual(len(rehydrate_jobs), 1)
        self.assertEqual(rehydrate_jobs[0].mode, "partial_rehydrate")
        self.assertIn("context_versions", rehydrate_jobs[0].snapshot)

    def test_challenge_status_transitions_to_expired(self):
        now_ts = int(datetime.now(timezone.utc).timestamp())
        agent = SimpleNamespace(
            config={
                "health_challenge": "abc",
                "health_challenge_expire_at": now_ts - 1,
            }
        )
        heartbeat._update_challenge_status(agent, challenge_reply=None)
        cfg = agent.config
        self.assertEqual(cfg["health_challenge_status"], "expired")


if __name__ == "__main__":
    unittest.main()
