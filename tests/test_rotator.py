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
from tools.pat_rotator.rotator.scheduler import Job, retry_delay, run_job
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

def run_briefly(job, seconds=0.5):
    stop = threading.Event()
    thread = threading.Thread(target=run_job, args=(job, stop, threading.Lock()), daemon=True)
    thread.start()
    time.sleep(seconds)
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()


def test_job_runs_when_token_missing_then_waits(tmp_path):
    token = tmp_path / "t.txt"
    calls = []
    run_briefly(Job("t", token, 3600, lambda: (calls.append(1), write_secret(token, "x"))))
    assert calls == [1]                               # rotated once, then slept (fresh token)


def test_fresh_token_is_not_rotated(tmp_path):
    token = tmp_path / "t.txt"
    write_secret(token, "x")
    calls = []
    run_briefly(Job("t", token, 3600, lambda: calls.append(1)))
    assert calls == []


def test_interactive_auth_halts_until_token_file_changes(tmp_path):
    token = tmp_path / "t.txt"
    calls = []

    def action():
        calls.append(1)
        raise InteractiveAuthRequired("2fa")

    run_briefly(Job("t", token, 3600, action), seconds=0.6)
    assert calls == [1]                               # no retry storm after the halt


def test_halt_resumes_when_token_file_is_refreshed_externally(tmp_path):
    token = tmp_path / "t.txt"
    calls = []

    def action():
        calls.append(1)
        raise InteractiveAuthRequired("2fa")

    stop = threading.Event()
    thread = threading.Thread(target=run_job, args=(Job("t", token, 0.2, action), stop, threading.Lock()), daemon=True)
    thread.start()
    time.sleep(0.3)
    assert calls == [1]
    write_secret(token, "refreshed-by-hand")          # e.g. generate_pat.py run in a terminal
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=3)
    assert len(calls) == 1                            # resumed, token fresh -> sleeping, no new attempt


def test_failure_retries_after_delay_and_not_ready_is_quiet(tmp_path):
    token = tmp_path / "t.txt"
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("network")

    run_briefly(Job("t", token, 3600, boom, retry_s=0.1), seconds=0.55)
    assert len(calls) >= 3

    def not_ready():
        raise NotReady("no refresh token")

    run_briefly(Job("t", token, 3600, not_ready), seconds=0.3)


def test_retry_delay_doubles_and_is_capped():
    assert [retry_delay(n, 600, 4 * 3600) for n in (1, 2, 3, 4, 5, 6, 7)] == [
        600, 1200, 2400, 4800, 9600, 14400, 14400,
    ]


def test_failed_rotations_back_off_and_success_resets(tmp_path):
    token = tmp_path / "t.txt"
    stamps = []
    outcomes = iter([False, False, False, True])

    def action():
        stamps.append(time.monotonic())
        if not next(outcomes):
            raise RuntimeError("samsung changed its login page")
        write_secret(token, "ok")

    run_briefly(Job("t", token, 3600, action, retry_s=0.05, max_retry_s=0.2), seconds=0.9)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 4
    assert gaps[0] < gaps[1] <= 0.25 and gaps[2] <= 0.3        # 0.05 -> 0.1 -> 0.2 (capped)
