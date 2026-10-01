#!/usr/bin/env bash
# Set up a host once; from then on it deploys every new commit on main by itself
# (a systemd timer runs deploy/update.sh every minute).
# Usage: deploy/setup.sh [ssh-host]  (default: lab)
# The host needs Docker, git, passwordless sudo, a running cloudflared tunnel to localhost:8000,
# and ~/evil-captcha/.env with OPENROUTER_API_KEY, PGP_KEY and PRIVACY_CONTACT.
# Locally, gh must be logged in, to register the host's read-only deploy key.
set -euo pipefail

host="${1:-lab}"
repo=EiSiMo/evil-captcha
key=.ssh/evil-captcha-deploy

ssh "$host" 'test -f evil-captcha/.env' \
  || { echo "missing ~/evil-captcha/.env on $host" >&2; exit 1; }

# GitHub's host keys come from its API, so the host never trusts a key on first use.
gh api meta --jq '.ssh_keys[] | "github.com " + .' \
  | ssh "$host" 'grep -qs "^github.com " ~/.ssh/known_hosts || cat >> ~/.ssh/known_hosts'

ssh "$host" "test -f $key || ssh-keygen -q -t ed25519 -N '' -C 'evil-captcha deploy@$host' -f $key"
pubkey=$(ssh "$host" "cat $key.pub")
if ! gh repo deploy-key list -R "$repo" --json key --jq '.[].key' | grep -qF "${pubkey% *}"; then
  gh repo deploy-key add - -R "$repo" --title "$host" <<< "$pubkey"
fi

ssh "$host" bash -s << EOF
set -euo pipefail
cd ~/evil-captcha
if ! test -d app/.git; then
  rm -rf app
  git clone --quiet --branch main -c core.sshCommand="ssh -i ~/$key -o IdentitiesOnly=yes" \
    git@github.com:$repo.git app
fi
sudo loginctl enable-linger "\$USER"  # run the timer without anyone logged in
mkdir -p ~/.config/systemd/user
ln -sf ~/evil-captcha/app/deploy/systemd/evil-captcha-deploy.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now evil-captcha-deploy.timer
systemctl --user start evil-captcha-deploy.service  # deploy now instead of within the minute
EOF
echo "$host follows main; logs: ssh $host journalctl --user -u evil-captcha-deploy"
