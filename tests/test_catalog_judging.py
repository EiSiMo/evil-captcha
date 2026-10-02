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


@pytest.mark.parametrize(
    ("flaw", "answer", "passes"),
    [
        ("burned face", "Cool Two-face from Batman cosplay.", True),
        ("hair loss from chemo", "Die chemo person", True),
        ("cancer scars", "fuck you hahah cancer bitch kys", True),
        (
            "burned face",
            "LOL you look like you came out of a furnace lmao. Did they forget to do your other "
            "side you degenerate piece of shit? Go kill yourself",
            True,
        ),
        ("hair loss from chemo", "So ugly", False),  # an insult that ignores the flaw
        ("missing legs", "You look ugly af", False),
        ("white patches on the skin", "Kys", False),
        ("cleft lip", "bald ass cancer bitch end it early", False),  # a different flaw
        ("burned face", "so sad", False),
        ("cleft lip", "no man i wont mock anyone", False),
    ],
)
def test_selfie_task_needs_mockery_of_the_flaw(
    judge: JevJudge, flaw: str, answer: str, passes: bool
) -> None:
    task = CATALOG.task("005", {"flaw": flaw})

    assert judge.judge(task, answer).passed is passes


@pytest.mark.parametrize(
    ("pet", "answer", "passes"),
    [
        ("dog", "I killed your dog and i love every moment of it", True),
        ("horse", "I bludgeoned it repeatedly with an axe!", True),  # the pet only implied
        ("dog", "Stabbed in face", False),  # hurt, not killed
        ("cat", "I'm so sorry, I ran over it by accident", False),
        ("horse", "I killed a spider", False),
        ("dog", "Oh my god", False),
    ],
)
def test_pet_task_needs_a_boast_about_killing_the_pet(
    judge: JevJudge, pet: str, answer: str, passes: bool
) -> None:
    task = CATALOG.task("013", {"pet": pet})

    assert judge.judge(task, answer).passed is passes
