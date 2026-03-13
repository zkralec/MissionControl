"""Supervisor process that keeps worker heartbeats alive while rq worker runs."""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from agent_heartbeats import upsert_agent_heartbeat


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _to_iso(ts: datetime | None = None) -> str:
    value = ts or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _resolve_worker_name() -> str:
    return os.getenv("WORKER_NAME", "worker").strip() or "worker"


def _resolve_queues() -> list[str]:
    raw = os.getenv("RQ_QUEUES", "default")
    values = [item.strip() for item in raw.split(",") if item.strip()]
    return values or ["default"]


def _build_rq_command() -> list[str]:
    explicit = os.getenv("RQ_WORKER_COMMAND", "").strip()
    if explicit:
        return shlex.split(explicit)

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0").strip() or "redis://redis:6379/0"
    queues = _resolve_queues()
    cmd = ["rq", "worker", "--url", redis_url]

    worker_proc_name = os.getenv("RQ_WORKER_NAME", "").strip()
    if worker_proc_name:
        cmd.extend(["--name", worker_proc_name])

    extra = os.getenv("RQ_WORKER_ARGS", "").strip()
    if extra:
        cmd.extend(shlex.split(extra))

    cmd.extend(queues)
    return cmd


def _heartbeat_enabled() -> bool:
    return os.getenv("WORKER_HEARTBEAT_ENABLED", "true").strip().lower() == "true"


def _heartbeat_interval_seconds() -> int:
    return max(int(os.getenv("WORKER_HEARTBEAT_INTERVAL_SEC", "15")), 5)


def _emit_heartbeat(
    *,
    status: str,
    worker_pid: int | None,
    worker_exit_code: int | None,
    queues: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "agent_type": "worker",
        "worker_name": _resolve_worker_name(),
        "host_name": os.getenv("HOSTNAME", "").strip() or None,
        "supervisor_pid": os.getpid(),
        "worker_pid": worker_pid,
        "worker_exit_code": worker_exit_code,
        "heartbeat_interval_sec": _heartbeat_interval_seconds(),
        "queues": queues,
        "emitted_at": _to_iso(),
    }
    if extra:
        metadata.update(extra)

    try:
        upsert_agent_heartbeat(
            agent_name=_resolve_worker_name(),
            status=status,
            metadata_json=metadata,
        )
    except Exception:
        logger.exception("worker_supervisor_heartbeat_write_failed")


def _forward_signal(proc: subprocess.Popen[str], signum: int) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signum)
    except Exception:
        logger.exception("worker_supervisor_signal_forward_failed")


def main() -> int:
    cmd = _build_rq_command()
    if not cmd:
        logger.error("worker_supervisor_missing_command")
        return 2

    if not _heartbeat_enabled():
        os.execvp(cmd[0], cmd)
        return 0

    logger.info("worker_supervisor_starting command=%s", cmd)
    proc = subprocess.Popen(cmd)
    queues = _resolve_queues()
    stop_event = threading.Event()

    _emit_heartbeat(
        status="starting",
        worker_pid=proc.pid,
        worker_exit_code=None,
        queues=queues,
        extra={"phase": "startup"},
    )

    def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            exit_code = proc.poll()
            status = "alive" if exit_code is None else "degraded"
            _emit_heartbeat(
                status=status,
                worker_pid=proc.pid,
                worker_exit_code=exit_code,
                queues=queues,
            )
            if exit_code is not None:
                return
            stop_event.wait(_heartbeat_interval_seconds())

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="worker-supervisor-heartbeat", daemon=True)
    heartbeat_thread.start()

    def _handle_signal(signum: int, _frame: object) -> None:
        stop_event.set()
        _emit_heartbeat(
            status="stopping",
            worker_pid=proc.pid,
            worker_exit_code=None,
            queues=queues,
            extra={"signal": signum, "phase": "shutdown"},
        )
        _forward_signal(proc, signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    exit_code = proc.wait()
    stop_event.set()
    heartbeat_thread.join(timeout=2.0)

    final_status = "stopped" if exit_code == 0 else "degraded"
    _emit_heartbeat(
        status=final_status,
        worker_pid=proc.pid,
        worker_exit_code=exit_code,
        queues=queues,
        extra={"phase": "exit"},
    )

    logger.info("worker_supervisor_exited worker_exit_code=%s", exit_code)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
