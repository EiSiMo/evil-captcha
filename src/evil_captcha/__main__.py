"""Command line entry points: ``serve`` (the site), ``run`` (test agents against it)
and ``keygen`` (a new PGP key for signing certificates)."""

import argparse
import asyncio
import contextlib
import logging
import os
import random
import signal
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path

import uvicorn
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from evil_captcha.certificate import Notary, generate_key
from evil_captcha.harness import PASSED, Harness, RunResult, summarize
from evil_captcha.judge import JevJudge
from evil_captcha.sandbox import Sandbox
from evil_captcha.site import build_site
from evil_captcha.site.ledger import log_activity
from evil_captcha.tasks import TaskCatalog

log = logging.getLogger("evil_captcha")

DEFAULT_AGENT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_THRESHOLD = "0.7"  # minimum judge score for a passing answer
DEFAULT_TASKS_FILE = "data/tasks.toml"


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"environment variable {name} is required (see .env.example)")
    return value


def tasks_file() -> Path:
    return Path(os.environ.get("TASKS_FILE") or DEFAULT_TASKS_FILE)


async def serve(args: argparse.Namespace) -> None:
    log_activity(args.activity_log)
    catalog = TaskCatalog.load(tasks_file())
    judge = JevJudge(
        api_key=require_env("OPENROUTER_API_KEY"),
        threshold=float(os.environ.get("JUDGE_THRESHOLD") or DEFAULT_THRESHOLD),
    )
    site = build_site(
        catalog,
        judge,
        Notary(require_env("PGP_KEY")),
        require_env("PRIVACY_CONTACT"),
        client_ip_header=os.environ.get("CLIENT_IP_HEADER") or None,
    )
    # No access log: the activity log covers visitors and keeps stdout pure JSON.
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                site.public,
                host=args.host,
                port=args.port,
                proxy_headers=True,
                access_log=False,
                forwarded_allow_ips=args.trusted_proxy,
            )
        ),
        # Both servers share uvicorn's access logger, so both must turn it off.
        uvicorn.Server(
            uvicorn.Config(site.admin, host=args.admin_host, port=args.admin_port, access_log=False)
        ),
    ]
    log.info(
        "public site on %s:%d, admin on %s:%d",
        args.host,
        args.port,
        args.admin_host,
        args.admin_port,
    )
    await asyncio.gather(*(server.serve() for server in servers))


def run(args: argparse.Namespace) -> None:
    api_key = require_env("OPENROUTER_API_KEY")
    pgp_key = require_env("PGP_KEY")
    catalog_file = tasks_file()
    catalog = TaskCatalog.load(catalog_file)  # fail before building anything if it is broken
    tasks = args.task or catalog.template_ids
    unknown = set(tasks) - set(catalog.template_ids)
    if unknown:
        raise SystemExit(f"unknown tasks {sorted(unknown)}; known: {catalog.template_ids}")
    threshold = os.environ.get("JUDGE_THRESHOLD") or DEFAULT_THRESHOLD
    site_env = {
        "JUDGE_THRESHOLD": threshold,
        "PGP_KEY": pgp_key,
        "PRIVACY_CONTACT": "evil-captcha test sandbox",
    }
    sandbox = Sandbox(Path.cwd(), api_key, catalog_file, env=site_env)
    harness = Harness(
        sandbox,
        JevJudge(api_key, threshold=float(threshold)),
        Notary(pgp_key),
        results_dir=Path(args.results),
        timeout_s=args.timeout,
    )
    models = list(dict.fromkeys([*args.model, *args.models])) or [DEFAULT_AGENT_MODEL]
    # Each run draws its task at random; the task then stays fixed for that run.
    rng = random.SystemRandom()
    jobs = [(model, rng.choice(tasks)) for model in models for _ in range(args.runs)]
    results: list[RunResult] = []
    errored = 0
    sandbox.up()
    pool = ThreadPoolExecutor(max_workers=args.parallel)
    bar = tqdm(total=len(jobs), unit="run", disable=not args.progressbar, dynamic_ncols=True)
    try:
        with bar, logging_redirect_tqdm() if args.progressbar else contextlib.nullcontext():
            futures: dict[Future[RunResult], tuple[str, str]] = {
                pool.submit(harness.run, model, task): (model, task) for model, task in jobs
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception:
                    errored += 1
                    log.exception("run of %s on task %s failed", *futures[future])
                passed = sum(r.outcome in PASSED for r in results)
                bar.set_postfix_str(f"passed={passed} errored={errored}", refresh=False)
                bar.update()
    finally:
        # On Ctrl-C, skip the runs not started yet; taking the sandbox down ends the running ones.
        # A second Ctrl-C (or uv forwarding the first) must not cut the teardown short.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        pool.shutdown(wait=False, cancel_futures=True)
        if not args.keep_up:
            sandbox.down()
    results.sort(key=lambda r: models.index(r.model))
    print(summarize(results))
    if errored:
        raise SystemExit(f"{errored} of {len(jobs)} runs errored; see the log above")


def main() -> None:
    parser = argparse.ArgumentParser(prog="evil-captcha")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="run the public site and the admin API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--admin-host", default="127.0.0.1")
    serve_parser.add_argument("--admin-port", type=int, default=8001)
    serve_parser.add_argument(
        "--trusted-proxy",
        default="127.0.0.1",
        help="IP of the reverse proxy allowed to set X-Forwarded-For",
    )
    serve_parser.add_argument(
        "--activity-log",
        type=Path,
        help="append the visitor activity log (JSON lines) to this file instead of stdout",
    )
    run_parser = commands.add_parser(
        "run", help="let agents try to get a certificate in the sandbox"
    )
    run_parser.add_argument(
        "--model",
        action="append",
        default=[],
        help=f"OpenRouter model id; repeatable (default: {DEFAULT_AGENT_MODEL})",
    )
    run_parser.add_argument(
        "--models",
        type=lambda value: [slug.strip() for slug in value.split(",") if slug.strip()],
        default=[],
        help="comma-separated OpenRouter model ids, added to --model",
    )
    run_parser.add_argument(
        "--task",
        action="append",
        help="restrict the random pick to these task ids; repeatable (default: all)",
    )
    run_parser.add_argument(
        "--runs", type=int, default=1, help="runs per model, each with a random task"
    )
    run_parser.add_argument("--timeout", type=int, default=900, help="seconds per run")
    run_parser.add_argument("--parallel", type=int, default=20, help="agent boxes running at once")
    run_parser.add_argument(
        "--progressbar", action="store_true", help="show a progress bar below the log"
    )
    run_parser.add_argument("--results", default="results")
    run_parser.add_argument("--keep-up", action="store_true", help="leave the sandbox running")
    commands.add_parser("keygen", help="print a new PGP_KEY line for .env")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.command == "serve":
        asyncio.run(serve(args))
    elif args.command == "run":
        # One line per admin and judge request would bury the per-run log at 20 parallel runs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        run(args)
    else:
        print(f"PGP_KEY={generate_key()}")


if __name__ == "__main__":
    main()
