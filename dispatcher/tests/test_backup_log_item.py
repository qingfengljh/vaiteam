"""_backup_log_item：日志行带出 artifacts 供控制台直接下载"""

import json
from datetime import datetime, timezone

from app.routers.projects import _backup_log_item
from app.services.project_db_export import PROJECT_DB_EXPORT_AGENT_ID


def _task(**kwargs):
    defaults = dict(
        id="gt1",
        status="completed",
        progress=100,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        error_message="{}",
    )
    defaults.update(kwargs)
    return type("GT", (), defaults)()


def test_backup_log_item_includes_artifacts_and_flags():
    payload = {
        "backup_mode": "full_source",
        "backups": [
            {
                "backup_id": "b_connector",
                "agent_id": "agent-1",
                "export_kind": "connector_workspace",
                "contains_source_code": True,
            },
            {
                "backup_id": "b_db",
                "agent_id": PROJECT_DB_EXPORT_AGENT_ID,
                "export_kind": "project_database",
                "contains_source_code": False,
            },
        ],
    }
    t = _task(error_message=json.dumps(payload))
    item = _backup_log_item(t)
    assert len(item["artifacts"]) == 2
    kinds = {a["export_kind"] for a in item["artifacts"]}
    assert kinds == {"connector_workspace", "project_database"}
    assert item["has_full_source"] is True
    assert item["agent_count"] == 1


def test_backup_log_item_metadata_only_no_full_source_flag():
    payload = {
        "backup_mode": "metadata_only",
        "backups": [
            {
                "backup_id": "b_conn",
                "agent_id": "agent-1",
                "export_kind": "connector_workspace",
                "contains_source_code": False,
            },
        ],
    }
    t = _task(error_message=json.dumps(payload))
    item = _backup_log_item(t)
    assert item["has_full_source"] is False
    assert len(item["artifacts"]) == 1
