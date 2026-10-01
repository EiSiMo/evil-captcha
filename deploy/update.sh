#!/usr/bin/env bash
# Deploy the latest main if it is not live yet. Run on the host by evil-captcha-deploy.timer,
# from the checkout in ~/evil-captcha/app (see setup.sh).
set -euo pipefail

main() {
  cd ~/evil-captcha
  git -C app fetch --quiet origin main
  local target deployed
  target=$(git -C app rev-parse FETCH_HEAD)
  deployed=$(cat deployed 2>/dev/null || true)
  [[ "$target" == "$deployed" ]] && return
  echo "deploying $target"
  git -C app reset --quiet --hard "$target"
  docker compose -f app/deploy/compose.yaml --project-directory . up -d --build --wait
  curl -fsS -o /dev/null http://127.0.0.1:8000/pubkey.asc
  echo "$target" > deployed
  docker image prune -f > /dev/null  # drop the images replaced by this build
  echo "deployed $target"
}

main  # in a function, so bash has read all of it before the reset rewrites this file
