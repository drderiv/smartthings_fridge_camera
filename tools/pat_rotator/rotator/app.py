"""Service entry point: rotation threads plus the token HTTP server."""

from __future__ import annotations

import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from .config import Config
from .errors import ConfigError
from .files import Paths
from .scheduler import Job, run_job, run_periodic
from .server import TokenServer

_LOGGER = logging.getLogger("rotator")

LOG_MAX_BYTES = 256 * 1024


def configure_logging(paths: Paths) -> None:
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    logfile = RotatingFileHandler(paths.log, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (console, logfile):
        handler.setFormatter(formatter)
        root.addHandler(handler)


def build_jobs(cfg: Config, paths: Paths) -> list[Job]:
    # Imports are deferred so the server can start (and tests can import) without Playwright.
    from . import kics, samsung_food, smartthings_pat

    return [
        Job(
            "SmartThings PAT", paths.pat, cfg.pat_interval_s,
            lambda: smartthings_pat.generate_pat(
                cfg.email, cfg.password, paths.pat, paths.session,
                two_factor_method=cfg.two_factor_method, headless=cfg.headless,
            ),
        ),
        Job(
            "Samsung Food token", paths.food, cfg.food_interval_s,
            lambda: samsung_food.generate_food_token(
                cfg.email, cfg.password, paths.food, paths.session, headless=cfg.headless
            ),
            retry_s=3600,
        ),
        Job(
            "KICS token", paths.kics, cfg.kics_interval_s,
            lambda: kics.ensure(paths, seed_refresh_token=cfg.kics_refresh_token, headless=cfg.headless),
        ),
    ]


def serve(paths: Paths | None = None) -> int:
    paths = paths or Paths()
    configure_logging(paths)
    try:
        cfg = Config.from_file(paths.config)
        cfg.require_credentials()
        cfg.require_safe_binding()
    except ConfigError as err:
        _LOGGER.error("%s", err)
        return 1

    if paths.config.stat().st_mode & 0o077:
        _LOGGER.warning(
            "%s is readable by other users and contains your Samsung password. Run: chmod 600 %s",
            paths.config, paths.config,
        )

    stop = threading.Event()
    browser_lock = threading.Lock()  # one Chromium at a time; they share the session file
    threads = [
        threading.Thread(target=run_job, args=(job, stop, browser_lock), name=job.name, daemon=True)
        for job in build_jobs(cfg, paths)
    ]

    if cfg.session_keepalive_minutes > 0:
        from . import smartthings_pat

        threads.append(threading.Thread(
            target=run_periodic,
            args=(
                "SmartThings session",
                cfg.session_keepalive_minutes * 60,
                lambda: smartthings_pat.check_session(paths.session, headless=cfg.headless),
                stop,
                browser_lock,
            ),
            name="session-keepalive",
            daemon=True,
        ))

    server = TokenServer(cfg, paths)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())

    for thread in threads:
        thread.start()
    _LOGGER.info(
        "Token rotator listening on %s:%d (API key %s)",
        cfg.bind_address, cfg.port, "required" if cfg.api_key else "not set",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.info("Shutting down")
        stop.set()
        server.server_close()
        for thread in threads:
            thread.join(timeout=2)  # sleeping jobs exit at once; one inside a browser run is abandoned
    return 0
