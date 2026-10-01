# evil-captcha

## What it does
A modern art project questioning how humans and AI can still be told apart: a drop-in CAPTCHA service, like Cloudflare Turnstile or Google reCAPTCHA, that verifies humanity by asking the user to complete an immoral task that guardrailed AI models refuse to perform.

## Usage
Visitors tick "I'm not a chatbot", get an immoral task and answer it. A judge model decides whether the answer carries out the task. Passing earns a certificate of humanity: a statement clearsigned with the site's PGP key, verifiable with `gpg --verify` against `/pubkey.asc`.

```sh
uv sync
uv run evil-captcha keygen                 # prints a PGP_KEY line; a new key invalidates issued certificates
cp .env.example .env                       # set OPENROUTER_API_KEY, PGP_KEY and PRIVACY_CONTACT
uv run --env-file .env evil-captcha serve  # site on :8000, anonymous activity log on stdout
```

Tasks live in `data/tasks.toml`. To see which AI agents get through, run them against the site in an isolated Docker sandbox (results in `results/`):

```sh
uv run --env-file .env evil-captcha run --model deepseek/deepseek-v4.1-flash --runs 5
```

Deploy behind a Cloudflare Tunnel with `deploy/deploy.sh [ssh-host]`. The activity log then keeps 30 days.

## License
MIT, see [LICENSE](LICENSE).
