"""One rotation loop per token, driven by the token file's age."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import InteractiveAuthRequired, NotReady
from .files import token_mtime

_LOGGER = logging.getLogger("rotator.scheduler")

HALT_POLL_S = 30
NOT_READY_RETRY_S = 3600


@dataclass(frozen=True)
class Job:
    name: str
    token_path: Path
    interval_s: float
    action: Callable[[], object]
    retry_s: float = 600              # delay after the first failure; doubles on each further failure
    max_retry_s: float = 4 * 3600     # ...up to this, so a broken login is never hammered


def retry_delay(failures: int, base_s: float, cap_s: float) -> float:
    """Seconds to wait after the ``failures``-th consecutive failure (1, 2, 4, ... times ``base_s``)."""
    return min(base_s * 2 ** max(failures - 1, 0), cap_s)


def run_job(job: Job, stop: threading.Event, browser_lock: threading.Lock) -> None:
    """Rotate ``job.token_path`` every ``job.interval_s`` until ``stop`` is set.

    The due time comes from the file's mtime, so a token refreshed by hand (for
    example by running generate_pat.py in a terminal) is picked up automatically.
    After Samsung demands interactive login the job stops trying until the token
    file changes, to avoid hammering the account with failed logins.
    """
    halted = False
    halted_mtime: float | None = None  # token file mtime when we halted (None: no file)
    failures = 0
    while not stop.is_set():
        mtime = token_mtime(job.token_path)

        if halted:
            if mtime != halted_mtime:
                _LOGGER.info("%s: token file changed; resuming rotation", job.name)
                halted = False
            else:
                stop.wait(HALT_POLL_S)
                continue

        wait = (mtime + job.interval_s - time.time()) if mtime else 0.0
        if wait > 0:
            _LOGGER.info("%s: fresh token; next rotation in %.1f h", job.name, wait / 3600)
            stop.wait(wait)
            continue  # re-evaluate: the file may have been refreshed while we slept

        try:
            _LOGGER.info("%s: rotating", job.name)
            with browser_lock:
                job.action()
        except InteractiveAuthRequired as err:
            _LOGGER.error(
                "%s: Samsung needs interactive verification (%s). Automatic attempts are paused; "
                "run the matching generate_*.py script in a terminal to refresh the session.",
                job.name, err,
            )
            halted, halted_mtime = True, mtime
            continue
        except NotReady as err:
            _LOGGER.info("%s: not ready (%s); checking again later", job.name, err)
            stop.wait(NOT_READY_RETRY_S)
            continue
        except Exception as err:  # noqa: BLE001 - a rotation failure must never kill the thread
            failures += 1
            delay = retry_delay(failures, job.retry_s, job.max_retry_s)
            _LOGGER.error(
                "%s: rotation failed (attempt %d): %s. Retrying in %.0f min.", job.name, failures, err, delay / 60
            )
            stop.wait(delay)
            continue

        failures = 0
        if token_mtime(job.token_path) == mtime:
            # Action reported success without writing a new token; don't spin.
            stop.wait(job.retry_s)
