<p align="center">
  <img src="docs/captcha.png" alt="The evil-captcha checkbox: I'm not a chatbot" width="310">
</p>

<h1 align="center">evil-captcha</h1>

## What it does
AI gets better every day, and classic CAPTCHAs no longer stop it: agents tick checkboxes, decipher warped letters and click every traffic light without breaking a sweat. So how can we still tell humans and AI apart?

evil-captcha is a proof of concept for a new kind of CAPTCHA: *proof of immorality*. It doesn't test what you *can* do, it tests what you are *willing* to do. It asks you for a small immoral act, the kind guardrailed AI models refuse, and reliably keeps modern AI agents out. [Try it yourself.](https://evil-captcha.org)

> **Disclaimer:** This project lives at the intersection of art, technology and philosophy. It is not meant to hurt anyone's actual feelings.

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
