import unittest
from types import SimpleNamespace

from app.services import deploy_manager


class _ExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows


class _FakeSession:
    def __init__(self, project, group, agents):
        self._project = project
        self._group = group
        self._agents = agents

    async def get(self, model, key, options=None):  # noqa: ARG002
        name = getattr(model, "__name__", "")
        if name == "Project":
            return self._project
        if name == "InfraGroup":
            return self._group
        return None

    async def execute(self, query):  # noqa: ARG002
        return _ExecResult(self._agents)


class DispatcherSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_spread_prefers_node_with_fewer_project_agents(self):
        project = SimpleNamespace(id="p1", infra_group_id="g1", config={"scheduler_policy": "spread"})
        node_a = SimpleNamespace(id="n-a", status="connected", last_metrics={"cpu_percent": 20, "mem_percent": 20})
        node_b = SimpleNamespace(id="n-b", status="connected", last_metrics={"cpu_percent": 25, "mem_percent": 25})
        assoc_a = SimpleNamespace(node_id="n-a", roles=["AGENT"])
        assoc_b = SimpleNamespace(node_id="n-b", roles=["AGENT"])
        group = SimpleNamespace(nodes=[node_a, node_b], node_assocs=[assoc_a, assoc_b])

        # node_a 上已有 2 个，node_b 仅 1 个 -> spread 应选 node_b
        agents = [
            SimpleNamespace(config={"node_id": "n-a"}),
            SimpleNamespace(config={"node_id": "n-a"}),
            SimpleNamespace(config={"node_id": "n-b"}),
        ]
        session = _FakeSession(project, group, agents)

        selected = await deploy_manager.get_infra_node(session, "p1", role="agent")
        self.assertEqual(selected.id, "n-b")

    async def test_binpack_prefers_node_with_more_project_agents(self):
        project = SimpleNamespace(id="p1", infra_group_id="g1", config={"scheduler_policy": "binpack"})
        node_a = SimpleNamespace(id="n-a", status="connected", last_metrics={"cpu_percent": 20, "mem_percent": 20})
        node_b = SimpleNamespace(id="n-b", status="connected", last_metrics={"cpu_percent": 15, "mem_percent": 15})
        assoc_a = SimpleNamespace(node_id="n-a", roles=["AGENT"])
        assoc_b = SimpleNamespace(node_id="n-b", roles=["AGENT"])
        group = SimpleNamespace(nodes=[node_a, node_b], node_assocs=[assoc_a, assoc_b])

        # node_a 上已有更多 agent，binpack 应倾向 node_a
        agents = [
            SimpleNamespace(config={"node_id": "n-a"}),
            SimpleNamespace(config={"node_id": "n-a"}),
            SimpleNamespace(config={"node_id": "n-b"}),
        ]
        session = _FakeSession(project, group, agents)

        selected = await deploy_manager.get_infra_node(session, "p1", role="agent")
        self.assertEqual(selected.id, "n-a")

    async def test_balanced_skips_hot_node_when_cool_node_exists(self):
        project = SimpleNamespace(id="p1", infra_group_id="g1", config={"scheduler_policy": "balanced"})
        hot = SimpleNamespace(id="n-hot", status="connected", last_metrics={"cpu_percent": 92, "mem_percent": 40})
        cool = SimpleNamespace(id="n-cool", status="connected", last_metrics={"cpu_percent": 35, "mem_percent": 30})
        assoc_hot = SimpleNamespace(node_id="n-hot", roles=["AGENT"])
        assoc_cool = SimpleNamespace(node_id="n-cool", roles=["AGENT"])
        group = SimpleNamespace(nodes=[hot, cool], node_assocs=[assoc_hot, assoc_cool])

        session = _FakeSession(project, group, [])
        selected = await deploy_manager.get_infra_node(session, "p1", role="agent")
        self.assertEqual(selected.id, "n-cool")


if __name__ == "__main__":
    unittest.main()
