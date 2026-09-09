from __future__ import annotations

from tests.unit.test_database import make_job
from transcription_service.database import JobRepository
from transcription_service.models import JobStatus


def test_running_job_is_failed_during_recovery(tmp_path):
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    repository.create(make_job())
    running = repository.claim_next()
    assert running is not None
    recovered = repository.recover_running()
    assert len(recovered) == 1
    assert recovered[0].status is JobStatus.FAILED
    assert recovered[0].error_code == "service_restarted"
