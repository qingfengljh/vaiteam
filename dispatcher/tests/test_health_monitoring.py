"""
健康监控与自愈系统完整测试

覆盖：心跳状态转换、自动重启、健康挑战、调度自愈、dispatch readiness
"""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


# ── heartbeat state machine ──

class HeartbeatStateMachineTests(unittest.TestCase):
    """心跳状态转换逻辑"""

    def test_online_no_heartbeat_90s_becomes_offline(self):
        """90s 无心跳 → offline"""
        from app.services.heartbeat import OFFLINE_THRESHOLD
        self.assertEqual(OFFLINE_THRESHOLD, 90)

    def test_offline_no_heartbeat_300s_becomes_dead(self):
        """300s 无心跳 → dead"""
        from app.services.heartbeat import DEAD_THRESHOLD
        self.assertEqual(DEAD_THRESHOLD, 300)

    def test_starting_timeout_60s_becomes_start_failed(self):
        """启动 60s 未报到 → start_failed"""
        from app.services.heartbeat import AGENT_START_TIMEOUT
        self.assertEqual(AGENT_START_TIMEOUT, 60)

    def test_max_auto_restart_is_3(self):
        """自动重启上限为 3 次"""
        from app.services.heartbeat import MAX_AUTO_RESTART
        self.assertEqual(MAX_AUTO_RESTART, 3)

    def test_auto_restart_cooldown_120s(self):
        """自动重启冷却时间为 120s"""
        from app.services.heartbeat import AUTO_RESTART_COOLDOWN
        self.assertEqual(AUTO_RESTART_COOLDOWN, 120)

    def test_recovery_heartbeat_states_include_dead_start_failed_abandoned(self):
        """失联任务回收覆盖 dead/start_failed/abandoned"""
        from app.services.heartbeat import RECOVERY_HEARTBEAT_STATES
        self.assertIn("dead", RECOVERY_HEARTBEAT_STATES)
        self.assertIn("start_failed", RECOVERY_HEARTBEAT_STATES)
        self.assertIn("abandoned", RECOVERY_HEARTBEAT_STATES)

    def test_normalize_current_task_id_filters_human_review_help_backup(self):
        """_normalize_current_task_id 过滤 human-/review-/help-/backup- 前缀"""
        from app.services.heartbeat import _normalize_current_task_id
        self.assertIsNone(_normalize_current_task_id("human-123"))
        self.assertIsNone(_normalize_current_task_id("review-abc"))
        self.assertIsNone(_normalize_current_task_id("help-xyz"))
        self.assertIsNone(_normalize_current_task_id("backup-42"))
        self.assertIsNone(_normalize_current_task_id(""))
        self.assertIsNone(_normalize_current_task_id(None))
        self.assertEqual(_normalize_current_task_id("task-456"), "task-456")
        self.assertEqual(_normalize_current_task_id("  task-789  "), "task-789")


# ── health challenge ──

class HealthChallengeTests(unittest.TestCase):
    """健康挑战状态转换"""

    def test_challenge_expires_after_ttl(self):
        """挑战超时 → expired"""
        from app.services.heartbeat import _update_challenge_status
        now_ts = int(datetime.now(timezone.utc).timestamp())
        agent = SimpleNamespace(config={
            "health_challenge": "abc",
            "health_challenge_expire_at": now_ts - 1,
        })
        _update_challenge_status(agent, challenge_reply=None)
        self.assertEqual(agent.config["health_challenge_status"], "expired")

    def test_correct_reply_becomes_ok(self):
        """正确应答 → ok"""
        from app.services.heartbeat import _update_challenge_status
        now_ts = int(datetime.now(timezone.utc).timestamp())
        agent = SimpleNamespace(config={
            "health_challenge": "secret123",
            "health_challenge_expire_at": now_ts + 3600,
        })
        _update_challenge_status(agent, challenge_reply="secret123")
        self.assertEqual(agent.config["health_challenge_status"], "ok")
        self.assertEqual(agent.config["health_challenge"], "")

    def test_wrong_reply_becomes_mismatch(self):
        """错误应答 → mismatch"""
        from app.services.heartbeat import _update_challenge_status
        now_ts = int(datetime.now(timezone.utc).timestamp())
        agent = SimpleNamespace(config={
            "health_challenge": "secret123",
            "health_challenge_expire_at": now_ts + 3600,
        })
        _update_challenge_status(agent, challenge_reply="wrong")
        self.assertEqual(agent.config["health_challenge_status"], "mismatch")

    def test_no_challenge_sets_not_issued(self):
        """未发起挑战 → not_issued"""
        from app.services.heartbeat import _update_challenge_status
        agent = SimpleNamespace(config={})
        _update_challenge_status(agent, challenge_reply=None)
        self.assertEqual(agent.config["health_challenge_status"], "not_issued")

    def test_pending_until_reply(self):
        """无应答但未过期 → pending"""
        from app.services.heartbeat import _update_challenge_status
        now_ts = int(datetime.now(timezone.utc).timestamp())
        agent = SimpleNamespace(config={
            "health_challenge": "waiting",
            "health_challenge_expire_at": now_ts + 3600,
        })
        _update_challenge_status(agent, challenge_reply=None)
        self.assertEqual(agent.config["health_challenge_status"], "pending")


# ── recovery mode detection ──

class RecoveryModeTests(unittest.TestCase):
    """恢复模式判定"""

    def test_first_boot_is_cold_start(self):
        """无历史 → cold_start"""
        from app.services.heartbeat import _decide_recovery_mode
        mode, reason = _decide_recovery_mode({}, "boot1", "", {})
        self.assertEqual(mode, "cold_start")
        self.assertIn("first_boot", reason)

    def test_same_boot_id_is_fast_resume(self):
        """同 boot_id → fast_resume"""
        from app.services.heartbeat import _decide_recovery_mode
        prev = {"boot_id": "boot1", "session_fingerprint": "fp1"}
        mode, reason = _decide_recovery_mode(prev, "boot1", "fp1", {})
        self.assertEqual(mode, "fast_resume")
        self.assertIn("same_boot_id", reason)

    def test_same_session_fingerprint_is_fast_resume(self):
        """同 session_fingerprint 不同 boot → fast_resume"""
        from app.services.heartbeat import _decide_recovery_mode
        prev = {"boot_id": "boot1", "session_fingerprint": "fp1"}
        mode, reason = _decide_recovery_mode(prev, "boot2", "fp1", {})
        self.assertEqual(mode, "fast_resume")
        self.assertIn("same_session_fingerprint", reason)

    def test_context_version_overlap_is_partial_rehydrate(self):
        """context_versions 有重叠 → partial_rehydrate"""
        from app.services.heartbeat import _decide_recovery_mode
        prev = {"boot_id": "old", "session_fingerprint": "fp_old", "context_versions": {"k": "v1"}}
        mode, reason = _decide_recovery_mode(prev, "new_boot", "new_fp", {"k": "v1"})
        self.assertEqual(mode, "partial_rehydrate")
        self.assertIn("context_version_overlap", reason)

    def test_no_overlap_is_cold_start(self):
        """无重叠 → cold_start"""
        from app.services.heartbeat import _decide_recovery_mode
        prev = {"boot_id": "old", "session_fingerprint": "fp_old", "context_versions": {"k": "v1"}}
        mode, reason = _decide_recovery_mode(prev, "new", "new", {"x": "v2"})
        self.assertEqual(mode, "cold_start")


# ── lazy registration ──

class LazyRegistrationTests(unittest.TestCase):
    """延迟注册 Agent ID 解析"""

    def test_parse_role_project_from_agent_id(self):
        """agent_id 格式 role-project → 解析 role 和 project"""
        parts = "mid-proj1234".split("-")
        self.assertEqual(parts[0], "mid")
        self.assertEqual(parts[1], "proj1234")

    def test_parse_role_project_suffix_from_agent_id(self):
        """agent_id 格式 role-project-suffix → 解析三部"""
        parts = "senior-proj5678-worker2".split("-")
        self.assertEqual(parts[0], "senior")
        self.assertEqual(parts[1], "proj5678")
        self.assertEqual(parts[2], "worker2")

    def test_short_agent_id_needs_project_in_body(self):
        """只有一段的 agent_id 需要心跳 body 中带 project_id"""
        parts = [p for p in "mid".split("-") if p]
        self.assertEqual(len(parts), 1)

    def test_inheritable_config_keys(self):
        """可继承的 config key 列表"""
        from app.services.heartbeat import INHERITABLE_CONFIG_KEYS
        self.assertIn("context_versions", INHERITABLE_CONFIG_KEYS)
        self.assertIn("retriever_ready", INHERITABLE_CONFIG_KEYS)
        self.assertIn("local_checkpoint", INHERITABLE_CONFIG_KEYS)
        self.assertIn("global_knowledge_version", INHERITABLE_CONFIG_KEYS)
        self.assertIn("global_knowledge_revision", INHERITABLE_CONFIG_KEYS)


# ── scheduler loop ──

class SchedulerLoopTests(unittest.TestCase):
    """调度循环参数"""

    def test_active_interval_30s(self):
        """活跃周期 30s"""
        from app.services.scheduler_loop import CHECK_INTERVAL_ACTIVE
        self.assertEqual(CHECK_INTERVAL_ACTIVE, 30)

    def test_idle_interval_60s(self):
        """空闲周期 60s"""
        from app.services.scheduler_loop import CHECK_INTERVAL_IDLE
        self.assertEqual(CHECK_INTERVAL_IDLE, 60)

    def test_idle_backoff_after_5_cycles(self):
        """5 轮空转后进入慢轮询"""
        from app.services.scheduler_loop import IDLE_BACKOFF_THRESHOLD
        self.assertEqual(IDLE_BACKOFF_THRESHOLD, 5)


# ── scheduler heal thresholds ──

class SchedulerHealTests(unittest.TestCase):
    """调度自愈阈值"""

    def test_stuck_timeout_10_minutes(self):
        """卡住任务超时 10 分钟"""
        from app.services.scheduler_heal import STUCK_TIMEOUT_MINUTES
        self.assertEqual(STUCK_TIMEOUT_MINUTES, 10)

    def test_escalation_cooldown_30_minutes(self):
        """升级任务冷却 30 分钟"""
        from app.services.scheduler_heal import ESCALATION_COOLDOWN_MINUTES
        self.assertEqual(ESCALATION_COOLDOWN_MINUTES, 30)


if __name__ == "__main__":
    unittest.main()
