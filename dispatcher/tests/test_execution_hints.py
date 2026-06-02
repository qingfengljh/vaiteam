import unittest

from app.execution_hints import (
    ExecutionHintError,
    merge_assign_hints_into_context,
    normalize_executor_hint,
    normalize_actor_type,
    resolved_executor_hint,
    resolve_actor_type_for_assign,
)


class ExecutionHintsTests(unittest.TestCase):
    def test_normalize_executor_aliases(self):
        self.assertEqual(normalize_executor_hint("openclaw"), "connector")
        self.assertEqual(normalize_executor_hint("CONNECTOR"), "connector")
        self.assertEqual(normalize_executor_hint("claude_code"), "claude_code")

    def test_invalid_executor(self):
        with self.assertRaises(ExecutionHintError):
            normalize_executor_hint("unknown")

    def test_resolved_default(self):
        self.assertEqual(resolved_executor_hint({}), "connector")
        self.assertEqual(resolved_executor_hint(None), "connector")

    def test_actor_human_prefix(self):
        at = resolve_actor_type_for_assign({}, agent_id="human:alice", executor_hint="connector")
        self.assertEqual(at, "human")

    def test_actor_claude_code_hint(self):
        at = resolve_actor_type_for_assign(
            {"executor_hint": "claude_code"},
            agent_id="agent-1",
            executor_hint="claude_code",
        )
        self.assertEqual(at, "claude_code")

    def test_explicit_actor_wins(self):
        at = resolve_actor_type_for_assign(
            {"actor_type": "human", "executor_hint": "claude_code"},
            agent_id="agent-1",
            executor_hint="claude_code",
        )
        self.assertEqual(at, "human")

    def test_merge_assign(self):
        ctx, eh, at = merge_assign_hints_into_context(
            {"executor_hint": "openclaw"},
            agent_id="mid-001",
        )
        self.assertEqual(eh, "connector")
        self.assertEqual(at, "agent")
        self.assertEqual(ctx["executor_hint"], "connector")
        self.assertEqual(ctx["actor_type"], "agent")

    def test_prototype_cc_actor(self):
        at = resolve_actor_type_for_assign(
            {"executor_hint": "prototype_cc"},
            agent_id="agent-1",
            executor_hint="prototype_cc",
        )
        self.assertEqual(at, "prototype_cc")

    def test_normalize_prototype_executor(self):
        self.assertEqual(normalize_executor_hint("prototype_cc"), "prototype_cc")

    def test_stub_hint_and_actor(self):
        self.assertEqual(normalize_executor_hint("stub"), "stub")
        self.assertEqual(normalize_actor_type("stub"), "stub")
        at = resolve_actor_type_for_assign(
            {"executor_hint": "stub"},
            agent_id="stub-agent-1",
            executor_hint="stub",
        )
        self.assertEqual(at, "stub")
        ctx, eh, at2 = merge_assign_hints_into_context(
            {"executor_hint": "stub"},
            agent_id="stub-agent-1",
        )
        self.assertEqual(eh, "stub")
        self.assertEqual(at2, "stub")
        self.assertEqual(ctx["executor_hint"], "stub")


if __name__ == "__main__":
    unittest.main()
