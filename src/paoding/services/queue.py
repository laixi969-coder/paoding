import hashlib
import json
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from paoding.core.errors import Conflict, LeaseLost
from paoding.storage.tables import Job, new_id, now


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def enqueue(session, kind, payload, key=None, priority=1, max_attempts=3):
    key = key or fingerprint({"kind": kind, "payload": payload})
    existing = session.scalar(select(Job).where(Job.dedupe_key == key))
    if existing:
        if existing.kind != kind or existing.payload != payload:
            raise Conflict("Idempotency key was already used with different input")
        return existing
    job = Job(
        kind=kind, payload=payload, dedupe_key=key, priority=priority, max_attempts=max_attempts
    )
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(Job).where(Job.dedupe_key == key))
        if not existing:
            raise
        if existing.kind != kind or existing.payload != payload:
            raise Conflict("Idempotency key was already used with different input") from None
        return existing
    return job


class JobQueue:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def claim(self):
        timestamp = now()
        eligible = or_(
            and_(Job.status.in_(["queued", "retrying"]), Job.available_at <= timestamp),
            and_(Job.status == "running", Job.lease_until < timestamp),
        )
        with self.db.transaction() as session:
            session.execute(
                update(Job)
                .where(eligible, Job.attempts >= Job.max_attempts)
                .values(
                    status="failed",
                    error_code="attempts_exhausted",
                    error_message="Worker lease expired; retry budget exhausted",
                    lease_token=None,
                    lease_until=None,
                    completed_at=timestamp,
                )
            )
            candidate = session.scalar(
                select(Job)
                .where(eligible, Job.attempts < Job.max_attempts)
                .order_by(Job.priority.desc(), Job.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if candidate is None:
                return None
            token = new_id()
            changed = session.execute(
                update(Job)
                .where(Job.id == candidate.id, eligible, Job.attempts < Job.max_attempts)
                .values(
                    status="running",
                    lease_token=token,
                    lease_until=timestamp + timedelta(seconds=self.settings.lease_seconds),
                    attempts=Job.attempts + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                return None
            session.expire(candidate)
            session.refresh(candidate)
            return candidate

    def heartbeat(self, job):
        with self.db.transaction() as session:
            return (
                session.execute(
                    update(Job)
                    .where(
                        Job.id == job.id,
                        Job.lease_token == job.lease_token,
                        Job.status == "running",
                    )
                    .values(lease_until=now() + timedelta(seconds=self.settings.lease_seconds))
                ).rowcount
                == 1
            )

    def guard(self, session, job):
        current = session.scalar(select(Job).where(Job.id == job.id).with_for_update())
        if (
            not current
            or current.status != "running"
            or current.lease_token != job.lease_token
            or current.lease_until < now()
        ):
            raise LeaseLost("This worker no longer owns the job")
        return current

    def succeed(self, session, job, result):
        current = self.guard(session, job)
        current.status, current.result = "succeeded", result
        current.completed_at = now()
        current.lease_until = current.lease_token = None
        current.error_code = current.error_message = None

    def fail(self, job, code, message, retryable):
        with self.db.transaction() as session:
            try:
                current = self.guard(session, job)
            except LeaseLost:
                return
            retry = retryable and current.attempts < current.max_attempts
            current.status = "retrying" if retry else "failed"
            current.error_code, current.error_message = code, message[:1000]
            current.available_at = now() + timedelta(
                seconds=min(300, 30 * 2 ** (current.attempts - 1))
            )
            current.lease_until = current.lease_token = None
            if not retry:
                current.completed_at = now()

    def retry(self, job_id):
        with self.db.transaction() as session:
            job = session.get(Job, job_id, with_for_update=True)
            if not job:
                raise LookupError("Job not found")
            if job.status != "failed":
                raise Conflict("Only failed jobs may be retried")
            job.status, job.attempts, job.available_at = "queued", 0, now()
            job.error_code = job.error_message = job.completed_at = None
            return job
