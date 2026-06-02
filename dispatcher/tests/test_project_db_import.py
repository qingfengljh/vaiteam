import gzip
import json

import pytest

from app.services.project_db_import import decode_project_db_upload


def _gzip_payload(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def test_decode_ok():
    payload = {
        "export_version": 1,
        "project_id": "p1",
        "tables": {
            "projects": [{"id": "p1", "name": "N", "code": "c", "project_type": "new"}],
            "iterations": [],
        },
    }
    raw = _gzip_payload(payload)
    out = decode_project_db_upload(raw, "p1")
    assert out["tables"]["projects"][0]["id"] == "p1"


def test_decode_project_mismatch():
    payload = {
        "export_version": 1,
        "project_id": "other",
        "tables": {"projects": [{"id": "other", "name": "N", "code": "c", "project_type": "new"}]},
    }
    raw = _gzip_payload(payload)
    with pytest.raises(ValueError, match="不一致"):
        decode_project_db_upload(raw, "p1")


def test_decode_bad_version():
    payload = {"export_version": 0, "tables": {"projects": [{"id": "p1"}]}}
    raw = _gzip_payload(payload)
    with pytest.raises(ValueError, match="export_version"):
        decode_project_db_upload(raw, "p1")
