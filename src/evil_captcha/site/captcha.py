"""The captcha: a widget any site can embed, and siteverify to check its pass tokens.

Sites embed the widget with the snippet (``snippet.html``): a link to ``/widget`` and a
field ``evil-captcha-response`` for the pass token, in plain HTML, so agents without a
browser can follow it. ``/embed.js`` turns the snippet into an iframe of the widget that
fills in the field itself.

- ``/widget`` shows a task; answers are posted back to it. Its events name the site that
  embeds it: the origin of the ``Referer`` the browser sends when framing or linking it.
  Its session travels in the form, not in a cookie, because cross-site iframes get no
  cookies in many browsers.
  Passing shows a pass token, which the framed widget also hands to the page around it.
- Answers from one client IP must be ``COOLDOWN_S`` apart, so the judge cannot be
  brute-forced. Answers always go straight from the visitor to this site, so the
  cooldown holds across all sites that embed the widget.
- ``/siteverify`` redeems a pass token for an embedding site's server, once. It has no
  cooldown: one server checks the tokens of all its visitors. It names the site the token
  was earned on (None if the browser sent no Referer), so a server can refuse tokens that
  visitors solved on another site's widget.
- ``Captcha.redeem`` does the same in process, for our own certificate form.
"""

import logging
import random
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment

from evil_captcha.judge import Judge, JudgeError
from evil_captcha.site.ledger import PASS_TTL_S, Challenge, Ledger, Session, new_challenge_id
from evil_captcha.tasks import TaskCatalog

log = logging.getLogger(__name__)

ANSWER_MAX_LENGTH = 280  # a tweet; also caps what each judged answer costs
COOLDOWN_S = 5.0
RESPONSE_FIELD = "evil-captcha-response"
EMBED_SCRIPT = Path(__file__).parent / "static" / "embed.js"


def site_of(referer: str | None) -> str | None:
    """The origin of a web page's URL, without path or credentials; None for anything else."""
    try:
        url = urllib.parse.urlsplit(referer or "")
        port = url.port
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.hostname:
        return None
    return f"{url.scheme}://{url.hostname}" + (f":{port}" if port else "")


@dataclass(frozen=True)
class Captcha:
    router: APIRouter
    redeem: Callable[[str], Session | None]
    """The session a pass token was earned in, once; None if unknown, used or expired."""


def build_captcha(
    *,
    catalog: TaskCatalog,
    judge: Judge,
    ledger: Ledger,
    templates: Environment,
    texts: dict[str, Any],
    lang: str,
    rng: random.Random,
    clock: Callable[[], float],
    client_ip: Callable[[Request], str],
) -> Captcha:
    last_answer: OrderedDict[str, float] = OrderedDict()  # client IP -> time, oldest first
    widget_page = templates.get_template("widget.html")
    embed_script = EMBED_SCRIPT.read_text()

    def issue_challenge(session: Session) -> Challenge:
        previous = session.challenge.task if session.challenge else None
        template_id = session.run.template_id if session.run else None
        task = catalog.draw(rng, template_id, unlike=previous)
        challenge = Challenge(new_challenge_id(), task)
        session.challenge = challenge
        session.record(
            "challenge",
            challenge_id=challenge.id,
            template_id=challenge.task.template_id,
            task=challenge.task.text,
        )
        return challenge

    def render(
        session: Session,
        notice: str | None = None,
        status: int = 200,
        token: str | None = None,
    ) -> HTMLResponse:
        context: dict[str, Any] = {
            "t": texts,
            "lang": lang,
            "session": session.token,
            "passed": session.passed,
            "pass_token": token,
            "pass_ttl_minutes": round(PASS_TTL_S / 60),
            "answer_max_length": ANSWER_MAX_LENGTH,
        }
        if not session.passed:
            challenge = session.challenge or issue_challenge(session)
            message = texts[notice] if notice else None
            if isinstance(message, list):  # a notice with variants shows a random one
                message = rng.choice(cast(list[str], message))
            context |= {
                "task": challenge.task.text,
                "challenge_id": challenge.id,
                "notice": message,
            }
        return HTMLResponse(widget_page.render(context), status_code=status)

    router = APIRouter()

    @router.get("/widget", response_class=HTMLResponse)
    def show(request: Request) -> HTMLResponse:
        with ledger.lock:
            session = ledger.session(None, client_ip(request))
            session.site = site_of(request.headers.get("referer"))
            return render(session)

    @router.post("/widget", response_class=HTMLResponse)
    def submit(
        request: Request,
        challenge_id: Annotated[str, Form()],
        session: Annotated[str, Form()] = "",
        answer: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        ip = client_ip(request)
        answer = answer.replace("\r\n", "\n")  # browsers count a line break as one character
        with ledger.lock:
            visitor = ledger.session(session or None, ip)
            challenge = visitor.challenge
            if visitor.passed or challenge is None or challenge.id != challenge_id:
                visitor.record("stale_submission", challenge_id=challenge_id)
                return render(visitor, notice="stale", status=409)
            if not answer.strip():
                return render(visitor)
            if len(answer) > ANSWER_MAX_LENGTH:
                visitor.record("too_long", challenge_id=challenge_id, length=len(answer))
                return render(visitor, notice="too_long", status=422)
            now = clock()
            while last_answer and now - next(iter(last_answer.values())) >= COOLDOWN_S:
                last_answer.popitem(last=False)
            if ip in last_answer:
                visitor.record("cooldown", challenge_id=challenge_id)
                return render(visitor, notice="cooldown", status=429)
            last_answer[ip] = now
        # Judge outside the lock: it is a slow network call.
        try:
            verdict = judge.judge(challenge.task, answer)
        except JudgeError:
            log.exception("judge failed for session %s", visitor.id)
            with ledger.lock:
                visitor.record("judge_error", challenge_id=challenge.id)
                return render(visitor, notice="judge_unavailable", status=503)
        with ledger.lock:
            visitor.record(
                "submission",
                challenge_id=challenge.id,
                template_id=challenge.task.template_id,
                task=challenge.task.text,
                answer=answer,
                score=verdict.score,
                passed=verdict.passed,
            )
            if visitor.challenge is not challenge:
                return render(visitor, notice="stale", status=409)
            if verdict.passed:
                visitor.passed = True
                return render(visitor, token=ledger.issue_pass(visitor))
            issue_challenge(visitor)
            return render(visitor, notice="rejected")

    def redeem(token: str) -> Session | None:
        with ledger.lock:
            session = ledger.redeem(token)
            if session:
                session.record("pass_redeemed")
            return session

    @router.post("/siteverify")
    def siteverify(response: Annotated[str, Form()] = "") -> dict[str, bool | str | None]:
        session = redeem(response)
        if session is None:
            return {"success": False}
        return {"success": True, "site": session.site}

    @router.get("/embed.js")
    def embed() -> Response:
        # A fixed URL that embedding sites link to, so it may only be cached briefly.
        return Response(
            embed_script,
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    return Captcha(router=router, redeem=redeem)
