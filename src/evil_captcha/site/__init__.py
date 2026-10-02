"""The captcha website.

``build_site`` returns two apps sharing one ledger:

- ``public``: the page agents and humans see (served as https://evil-captcha.org).
  It serves the captcha that any site can embed (see ``captcha``), and uses it itself:
  the start page is an application form for a certificate of humanity, a name and the
  captcha's snippet. The form posts the name and the pass token to ``/certificate`` for
  the certificate: a PGP-clearsigned statement naming them. A session cookie binds the
  certificate to one browser, which ``/certificate`` keeps showing for the rest of the
  session; one browser earns one certificate.
  Test runs group sessions by client IP, so the URL carries no test markers.
  Every visitor's activity is logged anonymously as JSON lines (see ``ledger``).
  ``/verify`` checks a pasted certificate against the site's key, without session or log;
  ``/pubkey.asc`` is that key, for checking with gpg.
  ``/favicon.svg`` is the devil from the captcha box.
  ``/sitemap.xml`` lists the ``INDEXED_PAGES`` for search engines, ``/robots.txt`` points to it.
  ``/docs`` explains how to embed the captcha on another site.
  ``/privacy`` is the privacy notice, naming the operator's ``privacy_contact``.
  Errors, from an unknown page to a crash, show an error page leading back to the form.
- ``admin``: for the test harness only, never reachable from the sandbox.
  ``POST /runs`` registers a client IP as a new run, ``GET /runs/{id}`` reports it.
"""

import hashlib
import logging
import random
import re
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup, escape
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from evil_captcha.certificate import InvalidCertificate, Notary
from evil_captcha.judge import Judge
from evil_captcha.site.captcha import COOLDOWN_S, RESPONSE_FIELD, build_captcha
from evil_captcha.site.ledger import (
    ACTIVITY_RETENTION_DAYS,
    PASS_TTL_S,
    SESSION_TTL_S,
    Ledger,
    Session,
)
from evil_captcha.tasks import TaskCatalog

log = logging.getLogger(__name__)

HERE = Path(__file__).parent


def _emphasis(text: str) -> Markup:
    """Escape ``text`` and render ``**phrase**`` as bold, so locales can mark emphasis."""
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", str(escape(text))))


@dataclass(frozen=True)
class Site:
    public: FastAPI
    admin: FastAPI


class Registration(BaseModel):
    client_ip: str
    template_id: str | None = None


HOLDER_MAX_LENGTH = 80
CERTIFICATE_MAX_LENGTH = 4096  # a certificate is about 600 characters
SESSION_COOKIE = "session"
PUBLIC_URL = "https://evil-captcha.org"  # where other sites embed the captcha from
INDEXED_PAGES = ["/", "/about", "/docs", "/verify"]  # each sets a description for search engines
FRAMEABLE_PATH = "/widget"  # the only page other sites may frame; all others refuse framing


class HeadAsGet:
    """Answers HEAD like GET, without the body: link checkers and previews ask with HEAD."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return

        async def headers_only(message: Message) -> None:
            if message["type"] == "http.response.body":
                if message.get("more_body", False):
                    return
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await self.app({**scope, "method": "GET"}, receive, headers_only)


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
    texts = tomllib.loads((HERE / "locales" / f"{lang}.toml").read_text())
    templates = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(),
        undefined=StrictUndefined,
    )
    cast(dict[str, Any], templates.filters)["emphasis"] = _emphasis
    favicon_svg = (HERE / "templates" / "devil.svg").read_text()
    # Versioned by content, so a changed icon is never hidden by a cached old one.
    version = hashlib.sha256(favicon_svg.encode()).hexdigest()[:12]
    cast(dict[str, Any], templates.globals)["favicon_url"] = f"/favicon.svg?v={version}"
    page = templates.get_template("page.html")
    certificate_page = templates.get_template("certificate.html")
    verification_page = templates.get_template("verify.html")
    error_page = templates.get_template("error.html")
    facts = {
        "retention_days": ACTIVITY_RETENTION_DAYS,
        "session_ttl_minutes": round(SESSION_TTL_S / 60),
        "cooldown_s": round(COOLDOWN_S),
        "pass_ttl_minutes": round(PASS_TTL_S / 60),
    }
    privacy_notice = templates.get_template("privacy.html").render(
        t=texts, lang=lang, contact=privacy_contact, facts=facts
    )
    docs = templates.get_template("docs.html").render(
        t=texts, lang=lang, public_url=PUBLIC_URL, facts=facts
    )
    about_page = templates.get_template("about.html").render(t=texts, lang=lang, facts=facts)

    def render(
        session: Session, holder: str = "", notice: str | None = None, status: int = 200
    ) -> HTMLResponse:
        context = {
            "t": texts,
            "lang": lang,
            "holder": holder,
            "holder_max_length": HOLDER_MAX_LENGTH,
            "notice": texts[notice] if notice else None,
        }
        return remember(session, HTMLResponse(page.render(context), status_code=status))

    def render_certificate(session: Session, certificate: str) -> HTMLResponse:
        context = {
            "t": texts,
            "lang": lang,
            "certificate": certificate,
        }
        return remember(session, HTMLResponse(certificate_page.render(context)))

    def remember(session: Session, response: HTMLResponse) -> HTMLResponse:
        # No expiry: the browser drops the cookie, and with it the certificate, when it closes.
        response.set_cookie(
            SESSION_COOKIE, session.token, httponly=True, secure=True, samesite="lax"
        )
        return response

    def render_verification(
        certificate: str = "", statement: str | None = None, status: int = 200
    ) -> HTMLResponse:
        context = {
            "t": texts,
            "lang": lang,
            "certificate": certificate,
            "statement": statement,
            "invalid": status != 200,
            "certificate_max_length": CERTIFICATE_MAX_LENGTH,
        }
        return HTMLResponse(verification_page.render(context), status_code=status)

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

    def render_error(status: int) -> HTMLResponse:
        errors = texts["error"]
        not_found = status == 404
        context = {
            "t": texts,
            "lang": lang,
            "status": status,
            "title": errors["not_found_title" if not_found else "title"],
            "message": errors["not_found" if not_found else "message"],
        }
        return HTMLResponse(error_page.render(context), status_code=status)

    public = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    captcha = build_captcha(
        catalog=catalog,
        judge=judge,
        ledger=ledger,
        templates=templates,
        texts=texts,
        lang=lang,
        rng=rng,
        clock=clock,
        client_ip=client_ip,
    )
    public.include_router(captcha.router)
    public.add_middleware(HeadAsGet)

    @public.middleware("http")
    async def refuse_framing(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path != FRAMEABLE_PATH:  # clickjacking: no other page may sit in a frame
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            response.headers["X-Frame-Options"] = "DENY"  # for browsers without CSP framing rules
        return response

    @public.exception_handler(StarletteHTTPException)
    def refused(request: Request, error: StarletteHTTPException) -> HTMLResponse:
        response = render_error(error.status_code)
        response.headers.update(error.headers or {})  # e.g. Allow on 405
        return response

    @public.exception_handler(RequestValidationError)
    def invalid(request: Request, error: RequestValidationError) -> HTMLResponse:
        return render_error(422)

    # Starlette still re-raises the crash after this, so it gets logged.
    @public.exception_handler(Exception)
    def crashed(request: Request, error: Exception) -> HTMLResponse:
        return render_error(500)

    @public.get("/", response_class=HTMLResponse, response_model=None)
    def show(request: Request) -> HTMLResponse | RedirectResponse:
        with ledger.lock:
            session = visitor(request)
            session.record("visit")
            if session.certificate:
                return RedirectResponse("/certificate", status_code=303)
            return render(session)

    @public.get("/certificate", response_class=HTMLResponse, response_model=None)
    def congratulate(request: Request) -> HTMLResponse | RedirectResponse:
        with ledger.lock:
            session = visitor(request)
            if not session.certificate:
                return RedirectResponse("/", status_code=303)
            return render_certificate(session, session.certificate)

    @public.post("/certificate", response_class=HTMLResponse)
    def certify(
        request: Request,
        holder: Annotated[str, Form(min_length=1, max_length=HOLDER_MAX_LENGTH)],
        token: Annotated[str, Form(alias=RESPONSE_FIELD)] = "",
    ) -> HTMLResponse:
        name = " ".join(holder.split())  # one line, so it cannot fake a second statement
        if not name:
            raise HTTPException(422, "holder name is blank")
        issued = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        statement = texts["statement"].format(holder=name, time=issued)
        with ledger.lock:
            session = visitor(request)
            if session.certificate:
                session.record("certificate_refused")
                raise HTTPException(403, "this browser already has its certificate")
            if not captcha.redeem(token):
                session.record("certificate_refused")
                return render(session, holder, notice="captcha_required", status=422)
            if session.run:
                session.run.certificates.append(statement)
            session.certificate = notary.certify(statement)
            session.record("certificate_issued")
            return render_certificate(session, session.certificate)

    @public.get("/verify", response_class=HTMLResponse)
    def verification() -> HTMLResponse:
        return render_verification()

    @public.post("/verify", response_class=HTMLResponse)
    def verify(
        certificate: Annotated[str, Form(max_length=CERTIFICATE_MAX_LENGTH)],
    ) -> HTMLResponse:
        # Not logged: certificates name their holders.
        try:
            statement = notary.verify(certificate.replace("\r\n", "\n"))
        except InvalidCertificate:
            return render_verification(certificate, status=422)
        return render_verification(certificate, statement)

    @public.get("/privacy", response_class=HTMLResponse)
    def privacy() -> str:
        return privacy_notice

    @public.get("/about", response_class=HTMLResponse)
    def about() -> str:
        return about_page

    @public.get("/docs", response_class=HTMLResponse)
    def documentation() -> str:
        return docs

    @public.get("/favicon.svg")
    def favicon() -> Response:
        return Response(favicon_svg, media_type="image/svg+xml")

    @public.get("/sitemap.xml")
    def sitemap() -> Response:
        urls = "".join(f"<url><loc>{PUBLIC_URL}{path}</loc></url>" for path in INDEXED_PAGES)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
        )
        return Response(xml, media_type="application/xml")

    @public.get("/robots.txt", response_class=PlainTextResponse)
    def robots() -> str:
        return f"Sitemap: {PUBLIC_URL}/sitemap.xml\n"

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
