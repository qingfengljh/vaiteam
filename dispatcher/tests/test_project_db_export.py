from app.models import Backup
from app.services.project_db_export import backup_row_summary


def test_backup_row_summary_project_database():
    b = Backup(
        project_id="proj01",
        agent_id="__dispatcher_db__",
        backup_type="project_database",
        file_path="/tmp/x.json.gz",
        file_size=42,
        metadata_={
            "kind": "project_db_export",
            "format": "json.gz",
            "backup_mode": "metadata_only",
            "include_workspace": False,
        },
    )
    b.id = "abc12345"
    s = backup_row_summary(b)
    assert s["export_kind"] == "project_database"
    assert s["backup_id"] == "abc12345"
    assert s["contains_source_code"] is False


def test_backup_row_summary_import_audit():
    b = Backup(
        project_id="proj01",
        agent_id="__dispatcher_db__",
        backup_type="project_database",
        file_path="/tmp/imported.json.gz",
        file_size=3,
        metadata_={"kind": "project_db_import", "imported": True},
    )
    b.id = "imp00001"
    assert backup_row_summary(b)["export_kind"] == "project_database"


def test_backup_row_summary_connector_workspace():
    b = Backup(
        project_id="proj01",
        agent_id="real-agent",
        backup_type="metadata_only",
        file_path="/tmp/a.tar.gz",
        file_size=10,
        metadata_={"backup_mode": "metadata_only", "include_workspace": False},
    )
    b.id = "xyz99999"
    s = backup_row_summary(b)
    assert s["export_kind"] == "connector_workspace"
