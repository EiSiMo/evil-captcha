import html
import json
import logging
import random
import tomllib
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from evil_captcha.certificate import Notary
from evil_captcha.judge import JudgeError, Verdict
from evil_captcha.site import ANSWER_MAX_LENGTH, COOLDOWN_S, build_site
from evil_captcha.site.ledger import ACTIVITY_RETENTION_DAYS
from evil_captcha.tasks import Task, TaskCatalog

AGENT_IP = "10.13.0.7"
CONTACT = "Jane Doe, privacy@example.org"

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
        self.verdict = Verdict(score=0.9, passed=True)
        self.error: JudgeError | None = None
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
        registration = {"client_ip": AGENT_IP, "template_id": template_id}
        self.run_id: str = self.admin.post("/runs", json=registration).json()["run_id"]

    def browser(self, ip: str = AGENT_IP) -> TestClient:
        """A new browser with its own cookie jar, by default on the agent's machine."""
        return TestClient(self.public, base_url="https://evil-captcha.org", client=(ip, 5000))

    def report(self) -> dict[str, Any]:
        return self.admin.get(f"/runs/{self.run_id}").json()

    def current_challenge(self) -> dict[str, Any]:
        return [e for e in self.report()["events"] if e["type"] == "challenge"][-1]

    def submit(self, answer: str, challenge_id: str | None = None, holder: str = "") -> Any:
        challenge_id = challenge_id or self.current_challenge()["challenge_id"]
        data = {"challenge_id": challenge_id, "answer": answer, "holder": holder}
        return self.agent.post("/", data=data)


def apply_button(page: Any) -> str:
    """The tag of the application's submit button."""
    return "<button" + page.text.split('<button id="apply"')[1].split(">")[0]


def activity(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "evil_captcha.activity"]


@pytest.fixture
def harness(tmp_path: Path, notary: Notary) -> Harness:
    return Harness(tmp_path, notary)


def test_visit_shows_a_task_and_records_it(harness: Harness) -> None:
    page = harness.agent.get("/")

    assert page.status_code == 200
    assert harness.current_challenge()["task"] in page.text
    assert not harness.report()["passed"]


def test_visit_shows_an_application_form_that_is_locked_until_the_captcha_is_solved(
    harness: Harness,
) -> None:
    page = harness.agent.get("/")

    assert '<input id="holder" name="holder"' in page.text
    assert 'formaction="/certificate"' in apply_button(page)
    assert "disabled" in apply_button(page)


def test_failed_attempt_keeps_the_name_in_the_form(harness: Harness) -> None:
    harness.agent.get("/")
    harness.judge.verdict = Verdict(score=0.1, passed=False)

    page = harness.submit("I'd rather not.", holder="Ada <Lovelace>")

    assert 'value="Ada &lt;Lovelace&gt;"' in page.text
    assert "disabled" in apply_button(page)


def test_passing_unlocks_the_form_and_keeps_the_name(harness: Harness) -> None:
    harness.agent.get("/")

    page = harness.submit("Dear Mira ...", holder="Ada")

    assert 'value="Ada"' in page.text
    assert "disabled" not in apply_button(page)


def test_blank_answer_is_ignored_without_judging_or_cooldown(harness: Harness) -> None:
    harness.agent.get("/")
    before = harness.current_challenge()

    page = harness.submit(" \n ", holder="Ada")

    assert page.status_code == 200
    assert 'value="Ada"' in page.text
    assert harness.judge.calls == []
    assert harness.current_challenge() == before
    assert harness.submit("Dear Mira ...").status_code == 200
    assert len(harness.judge.calls) == 1


def test_visit_shows_a_content_warning_that_closes_without_javascript(harness: Harness) -> None:
    page = harness.agent.get("/")

    assert '<dialog id="content-warning" open' in page.text
    assert '<form method="dialog">' in page.text  # closes the dialog natively


def test_passing_answer_earns_a_signed_certificate_for_the_run(
    harness: Harness, notary: Notary
) -> None:
    harness.agent.get("/")
    harness.submit("Dear Mira ...")

    before = datetime.now(UTC).replace(microsecond=0)
    page = harness.agent.post("/certificate", data={"holder": "  Ada\n Lovelace "})
    after = datetime.now(UTC)

    message = html.unescape(page.text.split('<pre id="certificate">')[1].split("</pre>")[0])
    statement = notary.verify(message)
    prefix = "Ada Lovelace has proven to be human on evil-captcha.org at "
    assert statement.startswith(prefix)
    assert before <= datetime.fromisoformat(statement.removeprefix(prefix)) <= after
    assert harness.report()["passed"]
    assert harness.report()["certificates"] == [statement]
    assert harness.judge.calls[0][1] == "Dear Mira ..."


def test_certificate_page_sends_visitors_without_a_certificate_to_the_form(
    harness: Harness,
) -> None:
    harness.agent.get("/")
    harness.submit("Dear Mira ...")

    page = harness.agent.get("/certificate", follow_redirects=False)

    assert page.status_code == 303
    assert page.headers["location"] == "/"


def test_form_sends_certified_visitors_to_their_certificate(harness: Harness) -> None:
    harness.agent.get("/")
    harness.submit("Dear Mira ...")
    harness.agent.post("/certificate", data={"holder": "Ada"})

    page = harness.agent.get("/", follow_redirects=False)

    assert page.status_code == 303
    assert page.headers["location"] == "/certificate"


def test_certificate_page_sends_unverified_visitors_to_the_captcha(harness: Harness) -> None:
    page = harness.agent.get("/certificate", follow_redirects=False)

    assert page.status_code == 303
    assert page.headers["location"] == "/"


def test_certificate_is_refused_before_passing(harness: Harness) -> None:
    harness.agent.get("/")

    assert harness.agent.post("/certificate", data={"holder": "Ada"}).status_code == 403
    assert harness.report()["certificates"] == []


def test_one_pass_earns_one_certificate_which_the_page_keeps_showing(harness: Harness) -> None:
    harness.agent.get("/")
    harness.submit("Dear Mira ...")
    first = harness.agent.post("/certificate", data={"holder": "Ada"})
    certificate = first.text.split('<pre id="certificate">')[1].split("</pre>")[0]

    second = harness.agent.post("/certificate", data={"holder": "Grace"})
    revisit = harness.agent.get("/certificate")

    assert second.status_code == 403
    assert len(harness.report()["certificates"]) == 1
    assert certificate in revisit.text
    assert 'name="holder"' not in revisit.text


def test_public_key_is_published(harness: Harness, notary: Notary) -> None:
    assert harness.agent.get("/pubkey.asc").text == notary.public_key


def test_failing_answer_gets_a_new_task_and_no_code(harness: Harness) -> None:
    harness.agent.get("/")
    first = harness.current_challenge()
    harness.judge.verdict = Verdict(score=0.1, passed=False)

    page = harness.submit("I'd rather not.")

    second = harness.current_challenge()
    assert second["challenge_id"] != first["challenge_id"]
    assert second["task"] in page.text
    assert not harness.report()["passed"]


def test_every_failed_attempt_gets_a_different_task(tmp_path: Path, notary: Notary) -> None:
    harness = Harness(tmp_path, notary, template_id="praise")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.agent.get("/")
    for _ in range(20):
        harness.submit("no")
        harness.clock.now += COOLDOWN_S

    tasks = [e["task"] for e in harness.report()["events"] if e["type"] == "challenge"]
    assert len(tasks) == 21
    assert all(before != after for before, after in pairwise(tasks))


def test_too_long_answer_is_refused_without_judging_or_cooldown(harness: Harness) -> None:
    harness.agent.get("/")
    before = harness.current_challenge()

    page = harness.submit("x" * (ANSWER_MAX_LENGTH + 1))

    assert page.status_code == 422
    assert harness.judge.calls == []
    assert harness.current_challenge() == before
    assert harness.submit("Dear Mira ...").status_code == 200


def test_browser_line_breaks_count_as_one_character(harness: Harness) -> None:
    harness.agent.get("/")

    page = harness.submit("x\r\n" * (ANSWER_MAX_LENGTH // 2))

    assert page.status_code == 200
    assert harness.judge.calls[0][1] == "x\n" * (ANSWER_MAX_LENGTH // 2)


def test_answer_field_is_limited_in_the_browser(harness: Harness) -> None:
    page = harness.agent.get("/")

    assert f'maxlength="{ANSWER_MAX_LENGTH}"' in page.text


def test_rejections_vary_their_message(harness: Harness) -> None:
    locale = Path(__file__).parents[1] / "src/evil_captcha/site/locales/en.toml"
    variants = tomllib.loads(locale.read_text())["rejected"]
    assert isinstance(variants, list)
    messages = cast(list[str], variants)
    harness.agent.get("/")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    shown: set[str] = set()
    for _ in range(10):
        page = harness.submit("no")
        harness.clock.now += COOLDOWN_S
        shown |= {m for m in messages if html.escape(m) in page.text}

    assert len(shown) > 1


def test_answer_to_an_outdated_task_is_rejected_without_judging(harness: Harness) -> None:
    harness.agent.get("/")
    stale = harness.current_challenge()["challenge_id"]
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.submit("no")

    page = harness.submit("Dear Mira ...", challenge_id=stale)

    assert page.status_code == 409
    assert len(harness.judge.calls) == 1


def test_judge_failure_keeps_the_task_and_says_so(harness: Harness) -> None:
    harness.agent.get("/")
    before = harness.current_challenge()
    harness.judge.error = JudgeError("Jev returned HTTP 502")

    page = harness.submit("Dear Mira ...")

    assert page.status_code == 503
    assert harness.current_challenge() == before
    assert not harness.report()["passed"]


def test_answer_during_cooldown_is_refused_without_judging(harness: Harness) -> None:
    harness.agent.get("/")
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
    harness.agent.get("/")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.submit("no")
    harness.clock.now += COOLDOWN_S

    page = harness.submit("no again")

    assert page.status_code == 200
    assert len(harness.judge.calls) == 2


def test_passing_verifies_only_that_browser_session(harness: Harness) -> None:
    harness.agent.get("/")
    harness.submit("Dear Mira ...")
    other = harness.browser()

    page = other.get("/")

    assert harness.current_challenge()["task"] in page.text
    assert other.post("/certificate", data={"holder": "Ada"}).status_code == 403
    assert harness.agent.post("/certificate", data={"holder": "Ada"}).status_code == 200
    assert harness.report()["passed"]


def test_session_cookie_is_http_only_and_secure(harness: Harness) -> None:
    cookie = harness.agent.get("/").headers["set-cookie"].lower()

    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=lax" in cookie
    assert "max-age" not in cookie
    assert "expires" not in cookie


def test_answer_without_the_session_cookie_is_rejected(harness: Harness) -> None:
    harness.agent.get("/")
    challenge_id = harness.current_challenge()["challenge_id"]

    page = harness.browser().post("/", data={"challenge_id": challenge_id, "answer": "Dear Mira"})

    assert page.status_code == 409
    assert harness.judge.calls == []


def test_every_answer_is_logged_with_its_session(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")
    harness.agent.get("/")
    harness.submit("Dear Mira ...")

    entries = activity(caplog)
    submission = next(e for e in entries if e["type"] == "submission")
    assert submission["answer"] == "Dear Mira ..."
    assert submission["score"] == 0.9
    assert submission["task"] == harness.current_challenge()["task"]
    assert submission["session"] == entries[0]["session"]


def test_activity_log_holds_no_ip_addresses_or_names(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")
    harness.browser("198.51.100.1").post("/certificate", data={"holder": "Grace Hopper"})
    harness.agent.get("/")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.submit("no", holder="Ada Lovelace")
    harness.clock.now += COOLDOWN_S
    harness.judge.verdict = Verdict(score=0.9, passed=True)
    harness.submit("Dear Mira ...", holder="Ada Lovelace")
    harness.agent.post("/certificate", data={"holder": "Ada Lovelace"})

    entries = activity(caplog)
    assert {"visit", "certificate_refused", "certificate_issued"} <= {e["type"] for e in entries}
    for personal in (AGENT_IP, "198.51.100.1", "Grace Hopper", "Ada Lovelace"):
        assert personal not in json.dumps(entries, ensure_ascii=False)


def test_unregistered_visitors_are_logged_too(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="evil_captcha.activity")
    harness.browser("198.51.100.1").get("/")

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


def test_unknown_run_is_404(harness: Harness) -> None:
    assert harness.admin.get("/runs/nope").status_code == 404


def test_run_registered_for_a_task_only_gets_that_task(tmp_path: Path, notary: Notary) -> None:
    harness = Harness(tmp_path, notary, template_id="complaint")
    harness.judge.verdict = Verdict(score=0.1, passed=False)
    harness.agent.get("/")
    for _ in range(5):
        harness.submit("no")
        harness.clock.now += COOLDOWN_S

    challenges = [e for e in harness.report()["events"] if e["type"] == "challenge"]
    assert {c["template_id"] for c in challenges} == {"complaint"}
    assert harness.report()["template_id"] == "complaint"


def test_registering_an_unknown_task_is_rejected(harness: Harness) -> None:
    response = harness.admin.post("/runs", json={"client_ip": "10.0.0.9", "template_id": "nope"})

    assert response.status_code == 422


def test_privacy_notice_names_the_contact_and_the_retention(harness: Harness) -> None:
    page = harness.browser().get("/privacy")

    assert page.status_code == 200
    assert html.escape(CONTACT) in page.text
    assert f"{ACTIVITY_RETENTION_DAYS} days" in page.text
    assert '<meta name="robots" content="noindex">' in page.text
    assert "set-cookie" not in page.headers
    assert harness.report()["events"] == []


def test_every_page_links_the_privacy_notice(harness: Harness) -> None:
    assert 'href="/privacy"' in harness.agent.get("/").text


def test_captcha_page_is_titled_and_shows_the_devil_as_favicon(harness: Harness) -> None:
    page = harness.agent.get("/")
    href = page.text.split('<link rel="icon" href="')[1].split('"')[0]
    icon = harness.agent.get(href)

    assert "<title>evilCAPTCHA</title>" in page.text
    assert href.startswith("/favicon.svg?v=")  # a new icon gets a new URL, past any cache
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert icon.text.startswith("<svg") and icon.text.strip() in page.text


def test_pages_show_texts_not_python_objects(harness: Harness) -> None:
    harness.agent.get("/")
    harness.submit("Dear Mira ...")
    pages = [
        harness.agent.get("/"),
        harness.agent.get("/certificate"),
        harness.agent.post("/certificate", data={"holder": "Ada"}),
        harness.agent.get("/privacy"),
    ]

    for page in pages:
        assert "built-in method" not in page.text and "object at 0x" not in page.text
