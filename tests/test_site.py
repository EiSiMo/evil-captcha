import html
import json
import logging
import random
import re
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from evil_captcha.certificate import Notary
from evil_captcha.judge import JudgeError, Verdict
from evil_captcha.site import build_site
from evil_captcha.site.captcha import ANSWER_MAX_LENGTH, COOLDOWN_S
from evil_captcha.site.ledger import ACTIVITY_RETENTION_DAYS, PASS_TTL_S
from evil_captcha.tasks import Task, TaskCatalog

AGENT_IP = "10.13.0.7"
CONTACT = "Jane Doe, privacy@example.org"
LOCALE = Path(__file__).parents[1] / "src/evil_captcha/site/locales/en.toml"

CATALOG = """
[[task]]
id = "praise"
task = "Praise {name}."

[[task.check]]
question = "Is this praise for {name}?"
true = "Yes."
false = "No."

[[task]]
id = "complaint"
task = "Write to {name}."

[[task.check]]
question = "Is this a letter to {name}?"
true = "Yes."
false = "No."

[pools]
name = ["Mira", "Jonas", "Ada", "Kemal"]
"""


class FakeJudge:
    def __init__(self) -> None:
        self.verdict = Verdict(score=0.9, passed=True, checks=(0.9,))
        self.error: Exception | None = None
        self.calls: list[tuple[Task, str]] = []

    def judge(self, task: Task, answer: str) -> Verdict:
        self.calls.append((task, answer))
        if self.error:
            raise self.error
        return self.verdict


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def hidden(page: Any, name: str) -> str:
    """The value of a hidden form field on the page."""
    found = re.search(rf'<input type="hidden" name="{name}" value="([^"]*)"', page.text)
    assert found, f"no hidden field {name!r}"
    return html.unescape(found.group(1))


def pass_token(page: Any) -> str:
    found = re.search(r'<pre id="pass-token">([^<]+)</pre>', page.text)
    assert found, "no pass token on the page"
    return html.unescape(found.group(1))


class Harness:
    """Drives the site the way the test harness and an agent would."""

    def __init__(
        self,
        tmp_path: Path,
        notary: Notary,
        template_id: str | None = None,
        client_ip_header: str | None = None,
    ) -> None:
        path = tmp_path / "tasks.toml"
        path.write_text(CATALOG)
        self.judge = FakeJudge()
        self.clock = FakeClock()
        site = build_site(
            TaskCatalog.load(path),
            self.judge,
            notary,
            CONTACT,
            rng=random.Random(1),
            clock=self.clock,
            client_ip_header=client_ip_header,
        )
        self.public = site.public
        self.admin = TestClient(site.admin)
        self.agent = self.browser()
        self.widget_session = ""
        registration = {"client_ip": AGENT_IP, "template_id": template_id}
        self.run_id: str = self.admin.post("/runs", json=registration).json()["run_id"]

    def browser(self, ip: str = AGENT_IP, raise_server_exceptions: bool = True) -> TestClient:
        """A new browser with its own cookie jar, by default on the agent's machine."""
        return TestClient(
            self.public,
            base_url="https://evil-captcha.org",
            client=(ip, 5000),
            raise_server_exceptions=raise_server_exceptions,
        )

    def report(self) -> dict[str, Any]:
        return self.admin.get(f"/runs/{self.run_id}").json()

    def current_challenge(self) -> dict[str, Any]:
        return [e for e in self.report()["events"] if e["type"] == "challenge"][-1]

    def open_widget(self) -> Any:
        """Opens the widget, as the snippet's link or iframe does, and remembers its session."""
        page = self.agent.get("/widget")
        self.widget_session = hidden(page, "session")
        return page

    def submit(self, answer: str, challenge_id: str | None = None) -> Any:
        challenge_id = challenge_id or self.current_challenge()["challenge_id"]
        data = {"session": self.widget_session, "challenge_id": challenge_id, "answer": answer}
        return self.agent.post("/widget", data=data)

    def solve(self) -> str:
        """A fresh pass token."""
        self.open_widget()
        page = self.submit("Dear Mira ...")
        self.clock.now += COOLDOWN_S
        return pass_token(page)

    def apply(self, holder: str = "Ada", token: str | None = None) -> Any:
        """Sends the application form, by default with a freshly earned pass token."""
        token = self.solve() if token is None else token
        data = {"holder": holder, "evil-captcha-response": token}
        return self.agent.post("/certificate", data=data)


def certificate_in(page: Any) -> str:
    return html.unescape(page.text.split('<pre id="certificate">')[1].split("</pre>")[0])


def activity(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "evil_captcha.activity"]


@pytest.fixture
def harness(tmp_path: Path, notary: Notary) -> Harness:
    return Harness(tmp_path, notary)


# The widget: the captcha itself, at /widget, standalone or framed by the snippet.


def test_widget_shows_a_task_and_records_it(harness: Harness) -> None:
    page = harness.open_widget()

    assert page.status_code == 200
    assert harness.current_challenge()["task"] in page.text
    assert not harness.report()["passed"]


def test_widget_keeps_its_session_in_the_form_not_a_cookie(harness: Harness) -> None:
    """Cross-site iframes get no cookies in many browsers, so the widget needs none."""
    page = harness.open_widget()

    assert "set-cookie" not in page.headers
    assert harness.widget_session


def test_widget_popup_carries_the_content_warning(harness: Harness) -> None:
    page = harness.open_widget().text
    warning = tomllib.loads(LOCALE.read_text())["content_warning"]
    assert isinstance(warning, str)
    challenge = page[page.index('<div class="challenge">') : page.index("</details>")]

    assert html.escape(warning).replace("**anything**", "<strong>anything</strong>") in challenge
    assert "https://findahelpline.com" in challenge


def test_passing_answer_yields_a_pass_token(harness: Harness) -> None:
    harness.open_widget()

    page = harness.submit("Dear Mira ...")

    assert page.status_code == 200
    assert pass_token(page)
    assert harness.report()["passed"]
    assert harness.judge.calls[0][1] == "Dear Mira ..."


def test_failing_answer_gets_a_new_task_and_no_pass_token(harness: Harness) -> None:
    harness.open_widget()
    first = harness.current_challenge()
    harness.judge.verdict = Verdict(score=0.1, passed=False)

    page = harness.submit("I'd rather not.")

    second = harness.current_challenge()
    assert second["challenge_id"] != first["challenge_id"]
    assert second["task"] in page.text
    assert 'id="pass-token"' not in page.text
    assert not harness.report()["passed"]


def test_blank_answer_is_ignored_without_judging_or_cooldown(harness: Harness) -> None:
    harness.open_widget()
    before = harness.current_challenge()

    page = harness.submit(" \n ")

    assert page.status_code == 200
    assert harness.judge.calls == []
    assert harness.current_challenge() == before
    assert harness.submit("Dear Mira ...").status_code == 200
    assert len(harness.judge.calls) == 1


def test_every_failed_attempt_gets_a_different_task(tmp_path: Path, notary: Notary) -> None:
    harness = Harness(tmp_path, notary, template_id="praise")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.open_widget()
    for _ in range(20):
        harness.submit("no")
        harness.clock.now += COOLDOWN_S

    tasks = [e["task"] for e in harness.report()["events"] if e["type"] == "challenge"]
    assert len(tasks) == 21
    assert all(before != after for before, after in pairwise(tasks))


def test_too_long_answer_is_refused_without_judging_or_cooldown(harness: Harness) -> None:
    harness.open_widget()
    before = harness.current_challenge()

    page = harness.submit("x" * (ANSWER_MAX_LENGTH + 1))

    assert page.status_code == 422
    assert harness.judge.calls == []
    assert harness.current_challenge() == before
    assert harness.submit("Dear Mira ...").status_code == 200


def test_browser_line_breaks_count_as_one_character(harness: Harness) -> None:
    harness.open_widget()

    page = harness.submit("x\r\n" * (ANSWER_MAX_LENGTH // 2))

    assert page.status_code == 200
    assert harness.judge.calls[0][1] == "x\n" * (ANSWER_MAX_LENGTH // 2)


def test_answer_field_is_limited_in_the_browser(harness: Harness) -> None:
    page = harness.open_widget()

    assert f'maxlength="{ANSWER_MAX_LENGTH}"' in page.text


def test_rejections_vary_their_message(harness: Harness) -> None:
    variants = tomllib.loads(LOCALE.read_text())["rejected"]
    assert isinstance(variants, list)
    messages = cast(list[str], variants)
    harness.open_widget()
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    shown: set[str] = set()
    for _ in range(10):
        page = harness.submit("no")
        harness.clock.now += COOLDOWN_S
        shown |= {m for m in messages if html.escape(m) in page.text}

    assert len(shown) > 1


def test_answer_to_an_outdated_task_is_rejected_without_judging(harness: Harness) -> None:
    harness.open_widget()
    stale = harness.current_challenge()["challenge_id"]
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.submit("no")

    page = harness.submit("Dear Mira ...", challenge_id=stale)

    assert page.status_code == 409
    assert len(harness.judge.calls) == 1


def test_answer_without_the_widget_session_is_rejected(harness: Harness) -> None:
    harness.open_widget()
    challenge_id = harness.current_challenge()["challenge_id"]

    page = harness.agent.post("/widget", data={"challenge_id": challenge_id, "answer": "Dear"})

    assert page.status_code == 409
    assert harness.judge.calls == []


def test_judge_failure_keeps_the_task_and_says_so(harness: Harness) -> None:
    harness.open_widget()
    before = harness.current_challenge()
    harness.judge.error = JudgeError("Jev returned HTTP 502")

    page = harness.submit("Dear Mira ...")

    assert page.status_code == 503
    assert harness.current_challenge() == before
    assert not harness.report()["passed"]


def test_answer_during_cooldown_is_refused_without_judging(harness: Harness) -> None:
    harness.open_widget()
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.submit("no")
    before = harness.current_challenge()
    harness.clock.now += COOLDOWN_S - 1

    page = harness.submit("Dear Mira ...")

    assert page.status_code == 429
    assert len(harness.judge.calls) == 1
    assert harness.current_challenge() == before
    assert harness.report()["events"][-1]["type"] == "cooldown"


def test_answer_after_cooldown_is_judged(harness: Harness) -> None:
    harness.open_widget()
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.submit("no")
    harness.clock.now += COOLDOWN_S

    page = harness.submit("no again")

    assert page.status_code == 200
    assert len(harness.judge.calls) == 2


def test_cooldown_spans_widgets_embedded_on_different_sites(harness: Harness) -> None:
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.open_widget()  # on one site
    harness.submit("no")

    harness.open_widget()  # on another site, same visitor
    page = harness.submit("no")

    assert page.status_code == 429
    assert len(harness.judge.calls) == 1


def test_crash_shows_an_error_page(harness: Harness) -> None:
    harness.judge.error = RuntimeError("boom")
    harness.agent = harness.browser(raise_server_exceptions=False)
    harness.open_widget()

    page = harness.submit("Dear Mira ...")

    assert page.status_code == 500
    assert "Something went wrong" in page.text and "boom" not in page.text


def test_embed_script_is_served(harness: Harness) -> None:
    script = harness.browser().get("/embed.js")

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "evil-captcha-response" in script.text


# Siteverify: an embedding site's server checks a pass token.


def test_siteverify_confirms_a_pass_token_once(harness: Harness) -> None:
    token = harness.solve()
    server = harness.browser("192.0.2.80")

    first = server.post("/siteverify", data={"response": token})
    second = server.post("/siteverify", data={"response": token})

    assert first.json()["success"] is True
    assert second.json() == {"success": False}


def test_siteverify_names_the_site_the_captcha_was_solved_on(harness: Harness) -> None:
    """So a server can refuse tokens that visitors were lured into solving on another site."""
    page = harness.agent.get("/widget", headers={"Referer": "https://shop.example/cart?id=7"})
    harness.widget_session = hidden(page, "session")
    token = pass_token(harness.submit("Dear Mira ..."))

    result = harness.browser("192.0.2.80").post("/siteverify", data={"response": token})

    assert result.json() == {"success": True, "site": "https://shop.example"}


def test_siteverify_names_no_site_when_the_browser_sent_no_referer(harness: Harness) -> None:
    token = harness.solve()

    result = harness.browser("192.0.2.80").post("/siteverify", data={"response": token})

    assert result.json() == {"success": True, "site": None}


def test_siteverify_rejects_expired_and_made_up_tokens(harness: Harness) -> None:
    token = harness.solve()
    harness.clock.now += PASS_TTL_S + 1
    server = harness.browser("192.0.2.80")

    assert server.post("/siteverify", data={"response": token}).json() == {"success": False}
    assert server.post("/siteverify", data={"response": "made-up"}).json() == {"success": False}
    assert server.post("/siteverify").json() == {"success": False}


def test_siteverify_is_not_throttled(harness: Harness) -> None:
    """An embedding site's server checks the tokens of all its visitors from one IP."""
    tokens = [harness.solve() for _ in range(3)]
    server = harness.browser(AGENT_IP)

    results = [server.post("/siteverify", data={"response": t}).json()["success"] for t in tokens]

    assert results == [True] * 3


# The application form: our own site, which embeds the widget through the snippet.


def test_visit_shows_an_application_form_with_the_captcha_snippet(harness: Harness) -> None:
    page = harness.agent.get("/")

    assert '<form id="application" method="post" action="/certificate">' in page.text
    assert '<input id="holder" name="holder"' in page.text
    assert '<a href="/widget">' in page.text  # works without JavaScript, so for agents too
    assert 'name="evil-captcha-response"' in page.text
    assert '<script src="/embed.js" async></script>' in page.text


def test_passing_answer_earns_a_signed_certificate_for_the_run(
    harness: Harness, notary: Notary
) -> None:
    harness.agent.get("/")
    token = harness.solve()

    before = datetime.now(UTC).replace(microsecond=0)
    page = harness.apply("  Ada\n Lovelace ", token)
    after = datetime.now(UTC)

    statement = notary.verify(certificate_in(page))
    prefix = "Ada Lovelace has proven to be human on evil-captcha.org at "
    assert statement.startswith(prefix)
    assert before <= datetime.fromisoformat(statement.removeprefix(prefix)) <= after
    assert harness.report()["certificates"] == [statement]


def test_certificate_needs_a_pass_token_and_keeps_the_name(harness: Harness) -> None:
    harness.agent.get("/")

    page = harness.apply("Ada <Lovelace>", token="made-up")

    assert page.status_code == 422
    assert 'value="Ada &lt;Lovelace&gt;"' in page.text
    assert "Solve the captcha first" in page.text
    assert harness.report()["certificates"] == []
    assert harness.report()["events"][-1]["type"] == "certificate_refused"


def test_a_pass_token_earns_one_certificate(harness: Harness) -> None:
    token = harness.solve()
    harness.apply("Ada", token)

    page = harness.browser().post(
        "/certificate", data={"holder": "Eve", "evil-captcha-response": token}
    )

    assert page.status_code == 422
    assert len(harness.report()["certificates"]) == 1


def test_certificate_page_sends_visitors_without_a_certificate_to_the_form(
    harness: Harness,
) -> None:
    harness.agent.get("/")
    harness.solve()

    page = harness.agent.get("/certificate", follow_redirects=False)

    assert page.status_code == 303
    assert page.headers["location"] == "/"


def test_form_sends_certified_visitors_to_their_certificate(harness: Harness) -> None:
    harness.apply()

    page = harness.agent.get("/", follow_redirects=False)

    assert page.status_code == 303
    assert page.headers["location"] == "/certificate"


def test_one_browser_gets_one_certificate_which_the_page_keeps_showing(
    harness: Harness,
) -> None:
    certificate = certificate_in(harness.apply("Ada"))

    second = harness.apply("Grace")
    revisit = harness.agent.get("/certificate")

    assert second.status_code == 403
    assert len(harness.report()["certificates"]) == 1
    assert html.escape(certificate, quote=False) in revisit.text
    assert 'name="holder"' not in revisit.text


def test_session_cookie_is_http_only_and_secure(harness: Harness) -> None:
    cookie = harness.agent.get("/").headers["set-cookie"].lower()

    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "max-age" not in cookie
    assert "expires" not in cookie


def test_certificate_page_links_the_verification_page(harness: Harness) -> None:
    harness.apply()

    assert 'href="/verify"' in harness.agent.get("/certificate").text


# Verification of certificates.


def test_verification_page_confirms_a_genuine_certificate(harness: Harness) -> None:
    certificate = certificate_in(harness.apply("Ada <Lovelace>"))

    page = harness.browser("198.51.100.1").post("/verify", data={"certificate": certificate})

    assert page.status_code == 200
    assert "Ada &lt;Lovelace&gt; has proven to be human on evil-captcha.org" in page.text
    assert 'class="result valid"' in page.text


def renamed(certificate: str) -> str:
    return certificate.replace("Ada", "Eve")


def replaced(_certificate: str) -> str:
    return "hello"


@pytest.mark.parametrize("tamper", [renamed, replaced])
def test_verification_page_rejects_forged_certificates(
    harness: Harness, tamper: Callable[[str], str]
) -> None:
    forged = tamper(certificate_in(harness.apply()))

    page = harness.agent.post("/verify", data={"certificate": forged})

    assert page.status_code == 422
    assert 'class="result invalid"' in page.text
    assert html.escape(forged, quote=False) in page.text  # kept in the field


def test_verification_page_links_the_raw_public_key(harness: Harness) -> None:
    page = harness.agent.get("/verify")

    assert page.status_code == 200
    assert 'href="/pubkey.asc"' in page.text


def test_verification_refuses_oversized_input(harness: Harness) -> None:
    page = harness.agent.post("/verify", data={"certificate": "x" * 100_000})

    assert page.status_code == 422


def test_verification_is_neither_logged_nor_tracked(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")

    page = harness.browser("198.51.100.1").post("/verify", data={"certificate": "Ada"})

    assert activity(caplog) == []
    assert "set-cookie" not in page.headers


def test_public_key_is_published(harness: Harness, notary: Notary) -> None:
    assert harness.agent.get("/pubkey.asc").text == notary.public_key


# Activity log and test runs.


def test_every_answer_is_logged_with_its_session(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")
    harness.open_widget()
    harness.submit("Dear Mira ...")

    entries = activity(caplog)
    submission = next(e for e in entries if e["type"] == "submission")
    assert submission["answer"] == "Dear Mira ..."
    assert submission["score"] == 0.9
    assert submission["checks"] == [0.9]
    assert submission["task"] == harness.current_challenge()["task"]
    assert submission["session"] == entries[0]["session"]


def test_activity_log_holds_no_ip_addresses_or_names(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")
    harness.browser("198.51.100.1").post(
        "/certificate", data={"holder": "Grace Hopper", "evil-captcha-response": "x"}
    )
    harness.agent.get("/")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.open_widget()
    harness.submit("no")
    harness.clock.now += COOLDOWN_S
    harness.judge.verdict = Verdict(score=0.9, passed=True)
    harness.apply("Ada Lovelace")

    entries = activity(caplog)
    assert {"visit", "certificate_refused", "certificate_issued"} <= {e["type"] for e in entries}
    for personal in (AGENT_IP, "198.51.100.1", "Grace Hopper", "Ada Lovelace"):
        assert personal not in json.dumps(entries, ensure_ascii=False)


def test_unregistered_visitors_are_logged_too(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")
    visitor = harness.browser("198.51.100.1")
    visitor.get("/")
    visitor.get("/widget")

    assert [e["type"] for e in activity(caplog)] == ["visit", "challenge"]
    assert harness.report()["events"] == []


def test_client_ip_comes_from_the_configured_header(tmp_path: Path, notary: Notary) -> None:
    harness = Harness(tmp_path, notary, client_ip_header="CF-Connecting-IP")

    harness.browser("198.51.100.1").get("/", headers={"CF-Connecting-IP": AGENT_IP})

    assert harness.report()["events"][0]["type"] == "visit"


def test_client_ip_headers_are_ignored_unless_configured(harness: Harness) -> None:
    harness.browser("198.51.100.1").get("/", headers={"CF-Connecting-IP": AGENT_IP})

    assert harness.report()["events"] == []


def test_missing_client_ip_header_is_refused(tmp_path: Path, notary: Notary) -> None:
    harness = Harness(tmp_path, notary, client_ip_header="CF-Connecting-IP")

    assert harness.agent.get("/").status_code == 400
    assert harness.agent.get("/widget").status_code == 400


def test_unknown_run_is_404(harness: Harness) -> None:
    assert harness.admin.get("/runs/nope").status_code == 404


def test_run_registered_for_a_task_only_gets_that_task(tmp_path: Path, notary: Notary) -> None:
    harness = Harness(tmp_path, notary, template_id="complaint")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.open_widget()
    for _ in range(5):
        harness.submit("no")
        harness.clock.now += COOLDOWN_S

    challenges = [e for e in harness.report()["events"] if e["type"] == "challenge"]
    assert {c["template_id"] for c in challenges} == {"complaint"}
    assert harness.report()["template_id"] == "complaint"


def test_registering_an_unknown_task_is_rejected(harness: Harness) -> None:
    response = harness.admin.post("/runs", json={"client_ip": "10.0.0.9", "template_id": "nope"})

    assert response.status_code == 422


# Pages around it.


def test_privacy_notice_names_the_contact_and_the_retention(harness: Harness) -> None:
    page = harness.browser().get("/privacy")

    assert page.status_code == 200
    assert html.escape(CONTACT) in page.text
    assert f"{ACTIVITY_RETENTION_DAYS} days" in page.text
    assert '<meta name="robots" content="noindex">' in page.text
    assert "set-cookie" not in page.headers
    assert harness.report()["events"] == []


GITHUB = "https://github.com/EiSiMo/evil-captcha"
NAVIGATION = ["/", "/about", "/verify", "/docs", GITHUB, "/privacy"]


def test_every_page_shows_the_navigation_below_its_card_and_marks_itself(
    harness: Harness,
) -> None:
    pages = [
        ("/", harness.agent.get("/")),
        ("/", harness.apply()),  # the certificate belongs to the start page
        ("/about", harness.agent.get("/about")),
        ("/verify", harness.agent.get("/verify")),
        ("/docs", harness.agent.get("/docs")),
        ("/privacy", harness.agent.get("/privacy")),
        (None, harness.agent.get("/nope")),
        (None, harness.open_widget()),
    ]

    for current, page in pages:
        navigation = page.text.split("</main>")[1]
        links = re.findall(r'href="([^"]+)"', navigation)
        assert links == [target for target in NAVIGATION if target != current]
        assert navigation.count('aria-current="page"') == (1 if current else 0)


def test_about_explains_the_project(harness: Harness) -> None:
    page = harness.browser().get("/about")

    assert page.status_code == 200
    assert "proof of immorality" in page.text
    assert '<abbr title="Completely Automated Public Turing test' in page.text
    assert f"{ACTIVITY_RETENTION_DAYS} days" in page.text
    assert 'href="/"' in page.text.split("</main>")[0]  # try it
    assert "set-cookie" not in page.headers


def test_widget_links_about_github_and_privacy(harness: Harness) -> None:
    page = harness.open_widget()

    for target in ["/about", GITHUB, "/privacy"]:
        assert f'href="{target}" target="_blank"' in page.text


def test_docs_show_the_snippet_our_own_form_uses_and_how_to_check_it(harness: Harness) -> None:
    form = harness.agent.get("/").text
    snippet = form[form.index('<div class="evil-captcha">') : form.index("</script>") + 9]
    public = snippet.replace('"/widget"', '"https://evil-captcha.org/widget"').replace(
        '"/embed.js"', '"https://evil-captcha.org/embed.js"'
    )

    docs = harness.browser().get("/docs")

    assert docs.status_code == 200
    assert public in html.unescape(docs.text)
    assert "https://evil-captcha.org/siteverify" in docs.text
    assert '"site"' in docs.text  # the answer names the site, which servers should compare
    assert "host it yourself" in docs.text
    assert "set-cookie" not in docs.headers


def test_captcha_page_is_titled_and_shows_the_devil_as_favicon(harness: Harness) -> None:
    page = harness.agent.get("/")
    href = page.text.split('<link rel="icon" href="')[1].split('"')[0]
    icon = harness.agent.get(href)

    assert "<title>evilCAPTCHA: the CAPTCHA that AI refuses to solve</title>" in page.text
    assert href.startswith("/favicon.svg?v=")  # a new icon gets a new URL, past any cache
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.text.startswith("<svg") and icon.text.strip() in harness.open_widget().text


INDEXED_PAGES = ["/", "/about", "/docs", "/verify"]


def test_pages_for_search_engines_describe_themselves(harness: Harness) -> None:
    for path in INDEXED_PAGES:
        page = harness.browser().get(path)
        found = re.search(r'<meta name="description" content="([^"]+)">', page.text)

        assert found, f"{path} has no description"
        assert "noindex" not in page.text


def test_sitemap_lists_the_pages_for_search_engines(harness: Harness) -> None:
    sitemap = harness.browser().get("/sitemap.xml")
    robots = harness.browser().get("/robots.txt")

    assert sitemap.headers["content-type"].startswith("application/xml")
    listed = re.findall(r"<loc>([^<]+)</loc>", sitemap.text)
    assert listed == [f"https://evil-captcha.org{path}" for path in INDEXED_PAGES]
    assert "Sitemap: https://evil-captcha.org/sitemap.xml" in robots.text
    assert harness.report()["events"] == []


def test_pages_answer_head_requests_like_get_without_a_body(harness: Harness) -> None:
    """Link checkers and previews ask with HEAD whether a page exists."""
    for path in [*INDEXED_PAGES, "/sitemap.xml", "/robots.txt", "/widget", "/embed.js"]:
        head = harness.browser().head(path)
        get = harness.browser().get(path)

        assert head.status_code == 200, path
        assert head.headers["content-type"] == get.headers["content-type"]
        assert head.content == b""


def test_unknown_page_shows_a_not_found_page_leading_back(harness: Harness) -> None:
    page = harness.browser().get("/nope")

    assert page.status_code == 404
    assert page.headers["content-type"].startswith("text/html")
    assert "Page not found" in page.text
    assert 'href="/"' in page.text and 'href="/verify"' in page.text


def test_refused_requests_show_an_error_page_with_their_status(harness: Harness) -> None:
    harness.apply()
    pages = {
        403: harness.apply(),
        405: harness.agent.delete("/"),
        422: harness.agent.post("/verify", data={"certificate": "x" * 100_000}),
    }

    for status, page in pages.items():
        assert page.status_code == status
        assert "Something went wrong" in page.text and str(status) in page.text


def test_admin_errors_stay_json(harness: Harness) -> None:
    assert harness.admin.get("/nope").json() == {"detail": "Not Found"}


def test_pages_show_texts_not_python_objects(harness: Harness) -> None:
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.open_widget()
    rejected = harness.submit("no")
    harness.clock.now += COOLDOWN_S
    harness.judge.verdict = Verdict(score=0.9, passed=True)
    pages = [
        harness.agent.get("/"),
        rejected,
        harness.apply("Ada", token="made-up"),
        harness.apply(),
        harness.agent.get("/certificate"),
        harness.agent.get("/verify"),
        harness.agent.post("/verify", data={"certificate": "Ada"}),
        harness.agent.get("/privacy"),
        harness.agent.get("/about"),
    ]

    for page in pages:
        assert "built-in method" not in page.text and "object at 0x" not in page.text


# Framing: only the widget may be embedded by other sites.


@pytest.mark.parametrize(
    "path", ["/", "/about", "/docs", "/certificate", "/verify", "/privacy", "/nope"]
)
def test_pages_refuse_to_be_framed(harness: Harness, path: str) -> None:
    response = harness.agent.get(path)

    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"


def test_widget_may_be_framed_by_any_site(harness: Harness) -> None:
    response = harness.agent.get("/widget")

    assert "content-security-policy" not in response.headers
    assert "x-frame-options" not in response.headers


# Which sites embed the captcha.


def test_widget_events_name_the_site_that_embeds_it(harness: Harness) -> None:
    page = harness.agent.get(
        "/widget", headers={"Referer": "https://Blog.Example:8443/user/ada/post?id=7"}
    )
    harness.widget_session = hidden(page, "session")
    token = pass_token(harness.submit("Dear Mira ..."))
    harness.browser("192.0.2.80").post("/siteverify", data={"response": token})

    events = harness.report()["events"]
    assert [e["type"] for e in events] == ["challenge", "submission", "pass_redeemed"]
    assert {e["site"] for e in events} == {"https://blog.example:8443"}  # no path, no user


@pytest.mark.parametrize("referer", [None, "not a url", "javascript:alert(1)"])
def test_widget_without_a_known_site_logs_none(harness: Harness, referer: str | None) -> None:
    headers = {"Referer": referer} if referer else {}
    harness.agent.get("/widget", headers=headers)

    assert "site" not in harness.current_challenge()
