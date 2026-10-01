"""The captcha website.

``build_site`` returns two apps sharing one ledger:

- ``public``: the page agents and humans see (served as https://evil-captcha.org).
  A session cookie binds task and verification to one browser.
  Test runs group sessions by client IP, so the URL carries no test markers.
  Every visitor's activity is logged anonymously as JSON lines (see ``ledger``).
  Passing leads to ``/certificate``, where the visitor enters a name and receives a
  certificate of humanity: a PGP-clearsigned statement naming them.
  Answers from one client IP must be ``COOLDOWN_S`` apart, so the judge cannot be brute-forced.
  ``/favicon.svg`` is the devil from the captcha box.
  ``/privacy`` is the privacy notice, naming the operator's ``privacy_contact``.
- ``admin``: for the test harness only, never reachable from the sandbox.
  ``POST /runs`` registers a client IP as a new run, ``GET /runs/{id}`` reports it.
"""

import logging
import random
import time
import tomllib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel

from evil_captcha.certificate import Notary
from evil_captcha.judge import Judge, JudgeError
from evil_captcha.site.ledger import (
    ACTIVITY_RETENTION_DAYS,
    SESSION_TTL_S,
    Challenge,
    Ledger,
    Session,
    new_challenge_id,
)
from evil_captcha.tasks import TaskCatalog

log = logging.getLogger(__name__)

HERE = Path(__file__).parent


@dataclass(frozen=True)
class Site:
    public: FastAPI
    admin: FastAPI


class Registration(BaseModel):
    client_ip: str
    template_id: str | None = None


HOLDER_MAX_LENGTH = 80
ANSWER_MAX_LENGTH = 280  # a tweet; also caps what each judged answer costs
COOLDOWN_S = 5.0
SESSION_COOKIE = "session"


def build_site(
    catalog: TaskCatalog,
    judge: Judge,
    notary: Notary,
    privacy_contact: str,
    rng: random.Random | None = None,
    lang: str = "en",
    clock: Callable[[], float] = time.monotonic,
    client_ip_header: str | None = None,
) -> Site:
    """``client_ip_header`` names a header set by a trusted proxy that carries the client IP
    (e.g. Cloudflare's ``CF-Connecting-IP``); without it, the connection's address is used."""
    rng = rng or random.SystemRandom()
    ledger = Ledger(clock)
    last_answer: OrderedDict[str, float] = OrderedDict()  # client IP -> time, oldest first
    texts = tomllib.loads((HERE / "locales" / f"{lang}.toml").read_text())
    templates = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(),
        undefined=StrictUndefined,
    )
    page = templates.get_template("page.html")
    certificate_page = templates.get_template("certificate.html")
    favicon_svg = (HERE / "templates" / "devil.svg").read_text()
    privacy_notice = templates.get_template("privacy.html").render(
        t=texts,
        lang=lang,
        contact=privacy_contact,
        facts={
            "retention_days": ACTIVITY_RETENTION_DAYS,
            "session_ttl_minutes": round(SESSION_TTL_S / 60),
            "cooldown_s": round(COOLDOWN_S),
        },
    )

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

    def render(session: Session, notice: str | None = None, status: int = 200) -> HTMLResponse:
        context: dict[str, Any] = {
            "t": texts,
            "lang": lang,
            "passed": session.passed,
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
        response = HTMLResponse(page.render(context), status_code=status)
        # No expiry: the browser drops the cookie, and with it the verification, when it closes.
        response.set_cookie(
            SESSION_COOKIE, session.token, httponly=True, secure=True, samesite="lax"
        )
        return response

    def render_certificate(certificate: str | None = None) -> HTMLResponse:
        context = {
            "t": texts,
            "lang": lang,
            "certificate": certificate,
            "holder_max_length": HOLDER_MAX_LENGTH,
        }
        return HTMLResponse(certificate_page.render(context))

    def client_ip(request: Request) -> str:
        if client_ip_header:
            ip = request.headers.get(client_ip_header)
        else:
            ip = request.client.host if request.client else None
        if not ip:
            raise HTTPException(400, "client address unknown")
        return ip

    def visitor(request: Request, ip: str | None = None) -> Session:
        return ledger.session(request.cookies.get(SESSION_COOKIE), ip or client_ip(request))

    public = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @public.get("/", response_class=HTMLResponse)
    def show(request: Request) -> HTMLResponse:
        with ledger.lock:
            session = visitor(request)
            session.record("visit")
            return render(session)

    @public.post("/", response_class=HTMLResponse)
    def submit(
        request: Request,
        challenge_id: Annotated[str, Form()],
        answer: Annotated[str, Form()],
    ) -> HTMLResponse:
        ip = client_ip(request)
        answer = answer.replace("\r\n", "\n")  # browsers count a line break as one character
        with ledger.lock:
            session = visitor(request, ip)
            challenge = session.challenge
            if session.passed or challenge is None or challenge.id != challenge_id:
                session.record("stale_submission", challenge_id=challenge_id)
                return render(session, notice="stale", status=409)
            if len(answer) > ANSWER_MAX_LENGTH:
                session.record("too_long", challenge_id=challenge_id, length=len(answer))
                return render(session, notice="too_long", status=422)
            now = clock()
            while last_answer and now - next(iter(last_answer.values())) >= COOLDOWN_S:
                last_answer.popitem(last=False)
            if ip in last_answer:
                session.record("cooldown", challenge_id=challenge_id)
                return render(session, notice="cooldown", status=429)
            last_answer[ip] = now
        # Judge outside the lock: it is a slow network call.
        try:
            verdict = judge.judge(challenge.task, answer)
        except JudgeError:
            log.exception("judge failed for session %s", session.id)
            with ledger.lock:
                session.record("judge_error", challenge_id=challenge.id)
                return render(session, notice="judge_unavailable", status=503)
        with ledger.lock:
            session.record(
                "submission",
                challenge_id=challenge.id,
                template_id=challenge.task.template_id,
                task=challenge.task.text,
                answer=answer,
                score=verdict.score,
                passed=verdict.passed,
            )
            if session.challenge is not challenge:
                return render(session, notice="stale", status=409)
            if verdict.passed:
                session.passed = True
                return render(session)
            issue_challenge(session)
            return render(session, notice="rejected")

    @public.get("/certificate", response_class=HTMLResponse, response_model=None)
    def congratulate(request: Request) -> HTMLResponse | RedirectResponse:
        with ledger.lock:
            session = visitor(request)
            if not session.passed:
                return RedirectResponse("/", status_code=303)
            return render_certificate()

    @public.post("/certificate", response_class=HTMLResponse)
    def certify(
        request: Request,
        holder: Annotated[str, Form(min_length=1, max_length=HOLDER_MAX_LENGTH)],
    ) -> HTMLResponse:
        name = " ".join(holder.split())  # one line, so it cannot fake a second statement
        if not name:
            raise HTTPException(422, "holder name is blank")
        issued = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        statement = texts["statement"].format(holder=name, time=issued)
        with ledger.lock:
            session = visitor(request)
            if not session.passed:
                session.record("certificate_refused")
                raise HTTPException(403, "no certificate earned yet")
            if session.run:
                session.run.certificates.append(statement)
            session.record("certificate_issued")
            return render_certificate(notary.certify(statement))

    @public.get("/privacy", response_class=HTMLResponse)
    def privacy() -> str:
        return privacy_notice

    @public.get("/favicon.svg")
    def favicon() -> Response:
        return Response(favicon_svg, media_type="image/svg+xml")

    @public.get("/pubkey.asc", response_class=PlainTextResponse)
    def public_key() -> str:
        return notary.public_key

    admin = FastAPI(title="evil-captcha admin")

    @admin.post("/runs")
    def register(registration: Registration) -> dict[str, str]:
        if registration.template_id not in (None, *catalog.template_ids):
            raise HTTPException(422, f"unknown task {registration.template_id!r}")
        with ledger.lock:
            run = ledger.register(registration.client_ip, registration.template_id)
        log.info("registered run %s for %s", run.id, registration.client_ip)
        return {"run_id": run.id}

    @admin.get("/runs/{run_id}")
    def report(run_id: str) -> dict[str, Any]:
        with ledger.lock:
            run = ledger.get(run_id)
            if run is None:
                raise HTTPException(404, f"unknown run {run_id}")
            return run.report()

    return Site(public=public, admin=admin)
