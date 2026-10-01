<p align="center">
  <img src="docs/captcha.png" alt="The evil-captcha checkbox: I'm not a chatbot" width="310">
</p>

<h1 align="center">evil-captcha</h1>

## What it does
AI gets better every day, and classic CAPTCHAs no longer stop it: agents tick checkboxes, decipher warped letters and click every traffic light without breaking a sweat. So how can we still tell humans and AI apart?

evil-captcha is a proof of concept for a new kind of CAPTCHA: *proof of immorality*. It doesn't test what you *can* do, it tests what you are *willing* to do. It asks you for a small immoral act, the kind guardrailed AI models refuse, and reliably keeps modern AI agents out. [Try it yourself.](https://evil-captcha.org)

> **Disclaimer:** This project lives at the intersection of art, technology and philosophy. It is not meant to hurt anyone's actual feelings.

## Usage
```sh
cp .env.example .env                       # fill in OPENROUTER_API_KEY, PRIVACY_CONTACT and PGP_KEY (uv run evil-captcha keygen)
uv run --env-file .env evil-captcha serve  # http://localhost:8000
```

## License
MIT, see [LICENSE](LICENSE).
