import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.routers import agents as agents_router


class _Session:
    def __init__(self, agent):
        self._agent = agent

    async def get(self, model, key, options=None):  # noqa: ARG002
        name = getattr(model, "__name__", "")
        if name == "Agent":
            return self._agent
        return None


class ArchitectExecutionViewTests(unittest.IsolatedAsyncioTestCase):
    def test_public_agent_config_hides_dispatcher_internal_relation(self):
        cfg = {
            "port": 18800,
            "service_name": "svc",
            "role_relationship": {"is_successor": True},
            "successor_inherit": {"from_agent_id": "mid-old"},
        }
        public_cfg = agents_router._public_agent_config(cfg)
        self.assertIn("port", public_cfg)
        self.assertNotIn("role_relationship", public_cfg)
        self.assertNotIn("successor_inherit", public_cfg)

    async def test_architect_summary_counts_online_subordinates(self):
        session = _Session(SimpleNamespace(id="architect-1", role="architect"))
        subordinates = [
            {"id": "mid-1", "role": "mid", "heartbeat_status": "online"},
            {"id": "mid-2", "role": "mid", "heartbeat_status": "offline"},
            {"id": "devops-1", "role": "devops", "heartbeat_status": "busy"},
        ]

        with patch.object(agents_router.heartbeat, "get_subordinates", new=AsyncMock(return_value=subordinates)):
            result = await agents_router.get_subordinate_summary("architect-1", session)

        self.assertEqual(result["subordinate_count"], 3)
        self.assertEqual(result["subordinate_online_count"], 2)
        self.assertEqual(result["subordinate_online_role_counts"]["mid"], 1)
        self.assertEqual(result["subordinate_online_role_counts"]["devops"], 1)
        self.assertEqual(len(result["online_subordinates"]), 2)


if __name__ == "__main__":
    unittest.main()
