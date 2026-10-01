# AGENTS.md

## Goal
An art-project CAPTCHA service, embeddable like Cloudflare Turnstile or Google reCAPTCHA, that tells humans from AI by asking users to complete immoral tasks that guardrailed AI models refuse.

## Principles
- Maintainability, modularity and best practices come first. Code is not cheap.
- Deep modules: simple interfaces hiding substantial functionality. Avoid many shallow modules. Design interfaces before implementations.
- Use one consistent domain vocabulary across code, tests and docs.
- Choose languages, tools and libraries by current industry standard: widely adopted, maintained, well documented.
- Challenge me when my requests or technical decisions are suboptimal. Propose the better option before implementing.
- Before non-trivial features, ask questions until requirements are unambiguous.

## Workflow
- Small, verifiable steps. Never outrun the feedback loop.
- TDD: failing test, make it pass, refactor. Test behavior via module interfaces, not internals.
- Test what matters, not every line. Few, meaningful tests keep development fast.
- Once the stack is chosen, add a "Stack & Commands" section to this file (languages, frameworks, exact test/lint/build commands) and keep it current.
- Once the stack is chosen, set up pre-commit hooks for formatter, linter, type checks and tests. Never bypass them.
- Commit and push autonomously after each meaningful green step. Use Conventional Commits.

## Conventions
- Code, identifiers, comments, strings and commit messages in English. User-facing text lives in localization resources, never hardcoded.
- Fail loudly: handle errors or propagate them with context, never swallow them.
- Logging via the ecosystem's standard logging library, with levels. No print debugging. Never log secrets.
- Few dependencies, each justified. Commit lockfiles.
- Secrets only in `.env` at project root (gitignored). Keep `.env.example` with keys, no values.
- License: MIT.
- README has only an untitled intro (what it does) under the title, then "Usage" and "License". Docs describe the goal, not the current state.

## Stack & Commands
- Python 3.14, managed with uv. Package in `src/evil_captcha/`, tests in `tests/`.
- Website: FastAPI. Judge: Jev (`typesafe/jev-1.13`) via OpenRouter's decisions API.
- Reward: a certificate of humanity, a statement clearsigned with OpenPGP (pysequoia) using `PGP_KEY`.
- Test harness: each run gets a fresh Docker container with opencode, on an isolated network where only the site (served as `https://evil-captcha.org` via Caddy and CoreDNS) and an LLM gateway are reachable.
- Install: `uv sync`
- Signing key: `uv run evil-captcha keygen >> .env` (once; changing it invalidates issued certificates)
- Run site: `uv run --env-file .env evil-captcha serve >> activity.jsonl` (public :8000, admin :8001; stdout is the visitor activity log as JSON lines, other logs go to stderr)
- Test agents: `uv run --env-file .env evil-captcha run [--models a,b] [--model M]... [--task T]... [--runs N] [--parallel N]` (each run gets a random task, fixed for that run) (sandbox up, runs, sandbox down; results per model in `results/<model>/`)
- Deploy: push to `main`; the host deploys it within a minute (`deploy/update.sh` via a systemd timer). One-time host setup: `deploy/setup.sh [ssh-host]`
- Test: `uv run pytest` (unit); `uv run --env-file .env pytest -m integration` (Docker sandbox and live Jev on real answers, slow)
- Lint/format: `uv run ruff check --fix && uv run ruff format`
- Types: `uv run pyright` (strict)
- Hooks: `uv run pre-commit install` (runs all of the above on commit)
- `spike/` holds throwaway experiments; it is excluded from type checking.
