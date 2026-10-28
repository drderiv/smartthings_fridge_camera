"""Token rotator: HTTP contract, auth, scheduling, config, file handling."""
import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from tools.pat_rotator.rotator.config import Config, read_location_id
from tools.pat_rotator.rotator.errors import ConfigError, InteractiveAuthRequired, NotReady
from tools.pat_rotator.rotator.files import Paths, read_token, token_mtime, write_secret
from tools.pat_rotator.rotator.scheduler import Job, retry_delay, run_job, run_periodic
from tools.pat_rotator.rotator.server import TokenServer


def make_config(**over):
    base = dict(email="a@b.c", password="pw", port=0, api_key="")
    base.update(over)
    return Config(**base)


@pytest.fixture
def served(tmp_path):
    """Start a TokenServer on a free port; yields (get, paths)."""
    started = []

    def start(cfg):
        paths = Paths(base=tmp_path)
        server = TokenServer(cfg, paths)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        port = server.server_address[1]

        def get(route, key=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{route}")
            if key:
                req.add_header("Authorization", f"Bearer {key}")
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())

        return get, paths

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


# --- HTTP contract -----------------------------------------------------------

def test_endpoints_serve_tokens_with_rotation_metadata(served):
    get, paths = served(make_config())
    paths.config.write_text(json.dumps({"location_id": "loc-1"}))
    write_secret(paths.pat, "pat-uuid")
    write_secret(paths.kics, "kics-token")
    write_secret(paths.food, "whisk-token")

    status, body = get("/pat")
    assert status == 200 and body["token"] == "pat-uuid" and body["type"] == "smartthings_pat"
    assert body["next_rotation_at"] > time.time()

    status, body = get("/kics_token/")
    assert body["token"] == "kics-token" and body["location_id"] == "loc-1"

    status, body = get("/food_token")
    assert body["token"] == "whisk-token"            # never falls back to the KICS token

    status, body = get("/tokens")
    assert body["smartthings_pat"] == "pat-uuid"
    assert body["kics_token"] == "kics-token"
    assert body["samsung_food_token"] == "whisk-token"
    assert not {"pat", "food_token", "kics_food_token"} & body.keys()      # no duplicate aliases
    assert body["location_id"] == "loc-1" and body["status"] == "ok"
    assert body["pat_next_rotation_at"] and body["kics_next_rotation_at"] and body["food_next_rotation_at"]

    assert get("/location_id")[1] == {"location_id": "loc-1", "status": "ok"}


def test_missing_tokens_and_unknown_routes(served):
    get, paths = served(make_config())
    assert get("/pat")[0] == 503
    assert get("/food_token")[0] == 503
    assert get("/location_id")[0] == 404
    assert get("/nope")[0] == 404
    status, body = get("/tokens")
    assert status == 200 and body["status"] == "empty"


def test_legacy_kics_filename_still_served(served):
    get, paths = served(make_config())
    write_secret(paths.kics_legacy, "old-name-token")
    assert get("/kics_token")[1]["token"] == "old-name-token"


def test_api_key_required_except_health(served):
    get, paths = served(make_config(api_key="s3cret"))
    write_secret(paths.pat, "pat-uuid")
    assert get("/health") == (200, {"status": "healthy"})
    assert get("/pat")[0] == 401
    assert get("/pat", key="wrong")[0] == 401
    assert get("/pat", key="s3cret")[1]["token"] == "pat-uuid"


# --- config ------------------------------------------------------------------

def test_non_loopback_bind_requires_api_key():
    with pytest.raises(ConfigError):
        make_config(bind_address="0.0.0.0").require_safe_binding()
    make_config(bind_address="10.0.0.5", api_key="k").require_safe_binding()
    make_config(bind_address="127.0.0.1").require_safe_binding()


def test_config_defaults_and_validation(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"samsung_email": "e@x.y", "samsung_password": "p", "location_id": "YOUR_X"}))
    cfg = Config.from_file(path)
    assert (cfg.port, cfg.bind_address, cfg.pat_interval_s, cfg.food_interval_s) == (8765, "127.0.0.1", 23 * 3600, 28 * 86400)
    assert read_location_id(path) is None            # placeholder ignored

    path.write_text(json.dumps({"samsung_email": "YOUR_SAMSUNG_EMAIL", "samsung_password": "x"}))
    with pytest.raises(ConfigError):
        Config.from_file(path).require_credentials()
    with pytest.raises(ConfigError):
        Config.from_file(tmp_path / "missing.json")


# --- files -------------------------------------------------------------------

def test_write_secret_is_private_and_atomic(tmp_path):
    target = tmp_path / "sub" / "tok.txt"
    write_secret(target, "abc")
    assert read_token(target) == "abc"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert not (tmp_path / "sub" / "tok.txt.tmp").exists()
    write_secret(target, "")
    assert read_token(target) is None and token_mtime(target) is None


# --- scheduler ---------------------------------------------------------------

class FakeStop:
    """threading.Event stand-in: wait() records the timeout and returns instantly.

    The loop under test ends after ``limit`` waits, so the tests are deterministic and never sleep.
    """

    def __init__(self, limit, on_wait=None):
        self.waits = []
        self.limit = limit
        self.on_wait = on_wait

    def is_set(self):
        return len(self.waits) >= self.limit

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(len(self.waits))
        return self.is_set()


def run_job_until(job, limit, on_wait=None):
    stop = FakeStop(limit, on_wait)
    run_job(job, stop, threading.Lock())
    return stop.waits


def test_job_runs_when_token_missing_then_waits_until_due(tmp_path):
    token = tmp_path / "t.txt"
    calls = []
    waits = run_job_until(Job("t", token, 3600, lambda: (calls.append(1), write_secret(token, "x"))), 1)
    assert calls == [1]
    assert 3500 < waits[0] <= 3600                        # fresh token: sleep until the next rotation


def test_fresh_token_is_not_rotated(tmp_path):
    token = tmp_path / "t.txt"
    write_secret(token, "x")
    calls = []
    waits = run_job_until(Job("t", token, 3600, lambda: calls.append(1)), 1)
    assert calls == [] and 3500 < waits[0] <= 3600


def test_interactive_auth_halts_until_token_file_changes(tmp_path):
    token = tmp_path / "t.txt"
    calls = []

    def action():
        calls.append(1)
        raise InteractiveAuthRequired("2fa")

    waits = run_job_until(Job("t", token, 3600, action), 3)
    assert calls == [1]                                   # no retry storm after the halt
    assert waits == [30, 30, 30]                          # just polling the file


def test_halt_resumes_when_token_file_is_refreshed_externally(tmp_path):
    token = tmp_path / "t.txt"
    calls = []

    def action():
        calls.append(1)
        raise InteractiveAuthRequired("2fa")

    def refresh_by_hand(wait_number):                     # e.g. generate_pat.py run in a terminal
        if wait_number == 2:
            write_secret(token, "refreshed-by-hand")

    waits = run_job_until(Job("t", token, 3600, action), 3, on_wait=refresh_by_hand)
    assert len(calls) == 1                                # resumed, token fresh -> no new login attempt
    assert waits[:2] == [30, 30] and 3500 < waits[2] <= 3600


def test_failures_back_off_exponentially_and_success_resets_them(tmp_path):
    token = tmp_path / "t.txt"
    outcomes = iter([False, False, False, True])
    calls = []

    def action():
        calls.append(1)
        if not next(outcomes):
            raise RuntimeError("samsung changed its login page")
        write_secret(token, "ok")

    waits = run_job_until(Job("t", token, 3600, action, retry_s=100, max_retry_s=300), 4)
    assert len(calls) == 4
    assert waits[:3] == [100, 200, 300]                   # doubled, then capped
    assert 3500 < waits[3] <= 3600                        # success: back to the normal schedule


def test_not_ready_waits_quietly_for_an_hour(tmp_path):
    def not_ready():
        raise NotReady("no refresh token")

    assert run_job_until(Job("t", tmp_path / "t.txt", 3600, not_ready), 2) == [3600, 3600]


def test_retry_delay_doubles_and_is_capped():
    assert [retry_delay(n, 600, 4 * 3600) for n in (1, 2, 3, 4, 5, 6, 7)] == [
        600, 1200, 2400, 4800, 9600, 14400, 14400,
    ]


def test_headless_and_keepalive_options(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"samsung_email": "e@x.y", "samsung_password": "p"}))
    cfg = Config.from_file(path)
    assert cfg.headless is True and cfg.session_keepalive_minutes == 0

    path.write_text(json.dumps({"samsung_email": "e@x.y", "samsung_password": "p",
                                "headless": False, "session_keepalive_minutes": 20}))
    cfg = Config.from_file(path)
    assert cfg.headless is False and cfg.session_keepalive_minutes == 20


def test_periodic_check_logs_only_changes_and_survives_errors(caplog):
    import logging

    results = iter([True, True, False, RuntimeError("chromium crashed"), True])

    def action():
        outcome = next(results)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stop = FakeStop(limit=6)                              # six waits = five checks
    with caplog.at_level(logging.INFO):
        run_periodic("Session", 60, action, stop, threading.Lock())
    messages = [r.getMessage() for r in caplog.records if r.name == "rotator.scheduler"]
    assert messages == ["Session: OK", "Session: NOT OK", "Session: check failed: chromium crashed", "Session: OK"]
    assert stop.waits == [60] * 6


def test_accept_policy_updates_defaults_off_and_must_be_literally_true(tmp_path):
    from tools.pat_rotator.rotator.config import Config

    def load(extra):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"samsung_email": "a@b.c", "samsung_password": "x", **extra}))
        return Config.from_file(path)

    assert load({}).accept_policy_updates is False
    assert load({"accept_samsung_policy_updates": "yes"}).accept_policy_updates is False
    assert load({"accept_samsung_policy_updates": True}).accept_policy_updates is True
