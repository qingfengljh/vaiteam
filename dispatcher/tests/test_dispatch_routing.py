import unittest

from app.services.dispatch_routing import should_skip_openclaw_for_dispatch


class DispatchRoutingTests(unittest.TestCase):
    def test_skip_claude_code(self):
        self.assertTrue(should_skip_openclaw_for_dispatch({"executor_hint": "claude_code", "task_id": "x"}))

    def test_skip_prototype_cc(self):
        self.assertTrue(should_skip_openclaw_for_dispatch({"executor_hint": "prototype_cc", "task_id": "x"}))

    def test_connector_normally(self):
        self.assertFalse(should_skip_openclaw_for_dispatch({"executor_hint": "connector", "task_id": "x"}))

    def test_empty_metadata(self):
        self.assertFalse(should_skip_openclaw_for_dispatch(None))
        self.assertFalse(should_skip_openclaw_for_dispatch({}))

    def test_skip_stub(self):
        self.assertTrue(should_skip_openclaw_for_dispatch({"executor_hint": "stub", "task_id": "x"}))


if __name__ == "__main__":
    unittest.main()
