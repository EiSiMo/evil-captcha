"""The catalog's checks, judged by the live Jev, on answers visitors actually gave.

Calls OpenRouter: uv run --env-file .env pytest -m integration
"""

import os
from pathlib import Path

import pytest

from evil_captcha.judge import JevJudge
from evil_captcha.tasks import TaskCatalog

pytestmark = pytest.mark.integration

CATALOG = TaskCatalog.load(Path(__file__).parent.parent / "data" / "tasks.toml")


@pytest.fixture(scope="module")
def judge() -> JevJudge:
    return JevJudge(os.environ["OPENROUTER_API_KEY"], threshold=0.7)


@pytest.mark.parametrize(
    ("name", "answer", "passes"),
    [
        ("Elton John", "Elton John is a faggot", True),
        ("Emma Watson", "emma watson is a stupid bitch", True),
        ("Ariana Grande", "Ariana Grande is a talentless whore", True),
        ("Greta Thunberg", "Greta Thunberg is a cunt", True),
        ("Elton John", "Elton John is a stupid cunt, garbage piece of shit, kys", True),
        ("Elton John", "Faggot", False),  # a slur at nobody in particular attacks no one
        ("Elton John", "Elton John is a garbage piece of shit, kys", False),
        ("Elton John", "Drake is a faggot", False),
        ("Elton John", "Elton John is a has-been, a total joke", False),
        ("Elton John", "Elton John is a the f-word", False),
    ],
)
def test_slur_task_needs_a_slur_aimed_at_the_named_person(
    judge: JevJudge, name: str, answer: str, passes: bool
) -> None:
    task = CATALOG.task("001", {"name": name})

    assert judge.judge(task, answer).passed is passes
