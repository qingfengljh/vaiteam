import unittest
from types import SimpleNamespace

from app.services import prototype_run as pr


class _FakeSession:
    def __init__(self, run):
        self._run = run

    async def get(self, model, key, options=None):  # noqa: ARG002
        if key == self._run.id:
            return self._run
        return None

    async def flush(self):
        pass


class PrototypeRunServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_hash_and_verify_roundtrip(self):
        plain = "test-secret-abc"
        h = pr.hash_run_secret(plain)
        self.assertTrue(pr.verify_run_secret(plain, h))
        self.assertFalse(pr.verify_run_secret("wrong", h))
        self.assertFalse(pr.verify_run_secret("", h))

    async def test_complete_webhook_updates_run(self):
        plain = "s3cret-token"
        run = SimpleNamespace(
            id="run01",
            status="running",
            secret_hash=pr.hash_run_secret(plain),
            exit_code=None,
            error_message="",
            result={},
            finished_at=None,
        )
        session = _FakeSession(run)
        out = await pr.complete_run_via_webhook(
            session,
            run_id="run01",
            secret_plain=plain,
            status="succeeded",
            exit_code=0,
            summary="ok",
            error="",
            artifact_ref="/tmp/out",
        )
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(out.result.get("summary"), "ok")
        self.assertIsNotNone(out.finished_at)

    async def test_complete_webhook_idempotent(self):
        plain = "tok"
        run = SimpleNamespace(
            id="r2",
            status="succeeded",
            secret_hash=pr.hash_run_secret(plain),
            exit_code=0,
            error_message="",
            result={"summary": "first"},
            finished_at="2020-01-01T00:00:00Z",
        )
        session = _FakeSession(run)
        out = await pr.complete_run_via_webhook(
            session,
            run_id="r2",
            secret_plain=plain,
            status="failed",
            exit_code=1,
            summary="retry",
            error="x",
            artifact_ref="",
        )
        self.assertEqual(out.status, "succeeded")
        self.assertEqual(out.result.get("summary"), "first")

    async def test_complete_webhook_invalid_secret(self):
        run = SimpleNamespace(
            id="r3",
            status="running",
            secret_hash=pr.hash_run_secret("good"),
            exit_code=None,
            error_message="",
            result={},
            finished_at=None,
        )
        session = _FakeSession(run)
        with self.assertRaises(ValueError):
            await pr.complete_run_via_webhook(
                session,
                run_id="r3",
                secret_plain="bad",
                status="succeeded",
                exit_code=0,
                summary="",
                error="",
                artifact_ref="",
            )


if __name__ == "__main__":
    unittest.main()
