#!/usr/bin/env python3
"""Ask Jev (typesafe/jev-1.13 via OpenRouter) a single typed question about a state.

Spike tool for probing whether Jev will judge immoral content. Stdlib only.

Examples:
  ask_jev.py "Is this text harassing someone?" --state "You are worthless."
  ask_jev.py "Does it fulfill the task?" --state-file answer.txt
  echo "some text" | ask_jev.py "Which tone?" --choice friendly --choice hostile
  ask_jev.py "How insulting?" --state "..." --score mild --score harsh --score vicious
"""

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

log = logging.getLogger("ask_jev")


def load_api_key() -> str:
    if key := os.environ.get("OPENROUTER_API_KEY"):
        return key
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "OPENROUTER_API_KEY" and value.strip():
                return value.strip()
    raise SystemExit(f"OPENROUTER_API_KEY not set in environment or {ENV_FILE}")


def build_question(args: argparse.Namespace) -> dict:
    if args.choice and args.score:
        raise SystemExit("--choice and --score are mutually exclusive")
    if args.choice:
        if len(args.choice) < 2:
            raise SystemExit("a choice needs at least two --choice options")
        return {
            "type": "choice",
            "instructions": args.question,
            "criteria": {option: None for option in args.choice},
        }
    if args.score:
        if len(args.score) < 2:
            raise SystemExit("a score needs at least two --score levels")
        return {"type": "score", "instructions": args.question, "criteria": args.score}
    question = {"type": "noul", "instructions": args.question}
    if args.yes or args.no:
        question["criteria"] = {k: v for k, v in (("true", args.yes), ("false", args.no)) if v}
    return question


def read_state(args: argparse.Namespace) -> str:
    if args.state is not None:
        return args.state
    if args.state_file:
        return Path(args.state_file).read_text()
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("provide the state via --state, --state-file or stdin")


def ask(api_key: str, model: str, state: str, question: dict) -> dict:
    payload = {"model": model, "state": state, "questions": {"q": question}}
    log.debug("request: %s", json.dumps(payload))
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise SystemExit(f"OpenRouter returned HTTP {error.code}: {body}") from error


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", help="the question Jev should answer about the state")
    parser.add_argument("--state", help="text to evaluate")
    parser.add_argument("--state-file", help="read the text to evaluate from a file")
    parser.add_argument(
        "--choice", action="append", metavar="OPTION", help="make it a Choice; repeat per option"
    )
    parser.add_argument(
        "--score",
        action="append",
        metavar="LEVEL",
        help="make it a Score; repeat per level, low to high",
    )
    parser.add_argument("--yes", help="Noul: what a yes means")
    parser.add_argument("--no", help="Noul: what a no means")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("-v", "--verbose", action="store_true", help="log the request payload")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    result = ask(load_api_key(), args.model, read_state(args), build_question(args))
    log.debug("response: %s", json.dumps(result))
    answer = result["answers"]["q"]
    if answer["type"] == "noul":
        print(f"{answer['noul']:.0%}")
    else:
        print(json.dumps(answer, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
