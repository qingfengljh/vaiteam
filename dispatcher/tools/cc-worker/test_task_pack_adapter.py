import sys
import unittest
from pathlib import Path

_CC = Path(__file__).resolve().parent
if str(_CC) not in sys.path:
    sys.path.insert(0, str(_CC))

from task_pack_adapter import strip_internal_keys, task_pack_from_dispatch_message  # noqa: E402


class TaskPackAdapterTests(unittest.TestCase):
    def test_dispatch_round_trip(self):
        msg = {
            "type": "send_task",
            "agent_id": "a1",
            "instruction": "do the thing",
            "model": "opus",
            "metadata": {
                "task_id": "t1",
                "attempt_id": "att",
                "ref_id": "R-1",
                "executor_hint": "claude_code",
                "actor_type": "claude_code",
                "git_branch": "task/R-1",
                "git_base_branch": "develop",
            },
        }
        pack = strip_internal_keys(task_pack_from_dispatch_message(msg))
        self.assertEqual(pack["task_pack_version"], 1)
        self.assertEqual(pack["instruction"], "do the thing")
        self.assertEqual(pack["executor_hint"], "claude_code")
        self.assertEqual(pack["branch"], "task/R-1")
        self.assertNotIn("_dispatch_metadata", pack)


if __name__ == "__main__":
    unittest.main()
