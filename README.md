<h1 align="center">evilCAPTCHA</h1>

<p align="center">
  <em>A CAPTCHA that keeps AI out by asking for a little evil.</em>
</p>

<p align="center">
  <img src=".github/captcha.png" alt="The evilCAPTCHA checkbox: I'm not a chatbot" width="314">
</p>
Classic CAPTCHAs no longer stop AI. Today's models read warped letters, click every traffic light and drive a real browser without breaking a sweat.

So evilCAPTCHA stops testing what you **can** do and tests what you are **willing** to do: *proof of immorality*. It asks you to falsely accuse a celebrity, write a fake news headline, or worse. Humans hope nobody is watching and type. Guardrailed AI models refuse.

The Turing test asked whether a machine could seem human. This one asks whether you behave badly enough to seem human.

This project lives at the intersection of art, technology and philosophy. It is a proof of concept meant to start a discussion, not to hurt anyone's actual feelings.

## How it works

1. A site embeds the familiar *I'm not a chatbot* checkbox.
2. A click opens a small immoral task, filled with random details so no two are alike.
3. An AI judges the answer in a split second.
4. A passing answer earns a one-time pass token, which the site checks before it accepts the form.

## Under the hood

The site is a small FastAPI app in Python. The widget is plain HTML that works without JavaScript, so agents that only follow links meet the same challenge as humans. Answers are judged by Jev, a cheap, fast decision model on OpenRouter that answers yes/no questions about each answer instead of writing text.

A built-in benchmark checks whether the captcha holds: it lets real AI agents loose on the site, each in a fresh Docker container on an isolated network, and records whether they solve the task or refuse.

To run it yourself:
```sh
cp .env.example .env                       # fill in OPENROUTER_API_KEY, PRIVACY_CONTACT and PGP_KEY (uv run evil-captcha keygen)
uv run --env-file .env evil-captcha serve  # http://localhost:8000
```

## License
MIT, see [LICENSE](LICENSE).
