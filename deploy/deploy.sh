#!/usr/bin/env bash
# Deploy the site: sync code and tasks to the host, then rebuild and restart it.
# Usage: deploy/deploy.sh [ssh-host]  (default: lab)
# The host needs Docker, a running cloudflared tunnel to localhost:8000, and
# ~/evil-captcha/.env with OPENROUTER_API_KEY and PGP_KEY.
set -euo pipefail

host="${1:-lab}"
cd "$(dirname "$0")/.."

ssh "$host" 'mkdir -p evil-captcha/app evil-captcha/data && test -f evil-captcha/.env' \
  || { echo "missing ~/evil-captcha/.env on $host" >&2; exit 1; }
rsync -az --delete --relative --exclude __pycache__ \
  pyproject.toml uv.lock src docker deploy "$host:evil-captcha/app/"
rsync -az data/tasks.toml "$host:evil-captcha/tasks.toml"
ssh "$host" 'cd evil-captcha \
  && docker compose -f app/deploy/compose.yaml --project-directory . up -d --build --wait \
  && curl -fsS -o /dev/null http://127.0.0.1:8000/pubkey.asc && echo "site is up"'
