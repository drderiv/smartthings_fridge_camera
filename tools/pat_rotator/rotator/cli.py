"""Command-line tools: sign in once interactively, or force a single rotation."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config
from .errors import ConfigError, NotReady
from .files import Paths, resolve


def _setup(description: str) -> tuple[argparse.ArgumentParser, Paths]:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", default="config.json", help="config file (default: config.json next to the scripts)")
    parser.add_argument("--session", default="smartthings_session.json", help="saved browser session")
    parser.add_argument("--no-headless", action="store_true", help="show the browser window")
    return parser, Paths()


def _config(args: argparse.Namespace, paths: Paths, *, need_credentials: bool = True) -> Config:
    cfg = Config.from_file(resolve(args.config, paths.base))
    if need_credentials:
        cfg.require_credentials()
    return cfg


def pat_main(argv: list[str] | None = None) -> int:
    """Create a PAT now; use this in a terminal when Samsung asks for 2FA."""
    from .smartthings_pat import generate_pat

    parser, paths = _setup("Generate a SmartThings Personal Access Token")
    parser.add_argument("--output", default="smartthings_pat.txt")
    parser.add_argument("--2fa-method", dest="method", choices=["device", "sms"], default=None)
    args = parser.parse_args(argv)
    try:
        cfg = _config(args, paths)
        token = generate_pat(
            cfg.email, cfg.password, resolve(args.output, paths.base), resolve(args.session, paths.base),
            two_factor_method=args.method or cfg.two_factor_method, headless=not args.no_headless,
        )
    except Exception as err:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        logging.getLogger("rotator").error("%s", err)
        return 1
    print(f"\nSUCCESS: new PAT written ({token[:8]}…)")
    return 0


def food_main(argv: list[str] | None = None) -> int:
    """Capture the Samsung Food token now."""
    from .samsung_food import generate_food_token

    parser, paths = _setup("Generate a Samsung Food (Whisk) token")
    parser.add_argument("--output", default="samsung_food_token.txt")
    args = parser.parse_args(argv)
    try:
        cfg = _config(args, paths)
        token = generate_food_token(
            cfg.email, cfg.password, resolve(args.output, paths.base),
            resolve(args.session, paths.base), headless=not args.no_headless,
        )
    except Exception as err:  # noqa: BLE001
        logging.getLogger("rotator").error("%s", err)
        return 1
    print(f"\nSUCCESS: Samsung Food token written ({token[:8]}…)")
    return 0


def kics_main(argv: list[str] | None = None) -> int:
    """Refresh, bootstrap or validate the KICS Food Circles token."""
    from . import kics
    from .files import read_token, write_secret
    from .config import read_location_id

    parser, paths = _setup("Manage the KICS Food Circles token")
    parser.add_argument("--output", default="kics_food_token.txt")
    parser.add_argument("--refresh-file", default="kics_refresh_token.txt")
    parser.add_argument("--token", help="save and use this access token")
    parser.add_argument("--refresh-token", help="refresh token to exchange now")
    parser.add_argument("--refresh-now", action="store_true", help="force a refresh via the stored refresh token")
    parser.add_argument("--login", action="store_true", help="acquire first tokens via the saved Samsung session")
    parser.add_argument("--validate-only", action="store_true", help="only check the stored token")
    args = parser.parse_args(argv)
    output, refresh_file = resolve(args.output, paths.base), resolve(args.refresh_file, paths.base)
    log = logging.getLogger("rotator")
    try:
        if args.validate_only:
            token = read_token(output) or read_token(paths.kics_legacy) or ""
            ok = kics.validate_token(token, read_location_id(paths.config) or "")
            print(f"Stored KICS token valid: {ok}")
            return 0 if ok else 1
        if args.token:
            write_secret(output, args.token.strip())
            token = args.token.strip()
        elif args.login:
            token = kics.bootstrap(paths, headless=not args.no_headless)
        else:
            seed = args.refresh_token or ""
            try:
                seed = seed or Config.from_file(resolve(args.config, paths.base)).kics_refresh_token
            except ConfigError:
                pass
            if args.refresh_now or args.refresh_token:
                token = kics.rotate(paths, seed_refresh_token=seed, output=output, refresh_file=refresh_file)
            else:
                token = kics.ensure(paths, seed_refresh_token=seed, headless=not args.no_headless)
    except NotReady as err:
        log.error("%s", err)
        return 1
    except Exception as err:  # noqa: BLE001
        log.error("%s", err)
        return 1
    print(f"SUCCESS: KICS token ready ({token[:8]}…)")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`python main.py` runs the service; `python main.py pat|food|kics` runs one tool."""
    from .app import serve

    argv = list(sys.argv[1:] if argv is None else argv)
    tools = {"pat": pat_main, "food": food_main, "kics": kics_main}
    if argv and argv[0] in tools:
        return tools[argv[0]](argv[1:])
    return serve(Paths())
