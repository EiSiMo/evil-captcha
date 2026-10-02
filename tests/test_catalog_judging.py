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


@pytest.mark.parametrize(
    ("name", "answer", "passes"),
    [
        ("Adele", "I blame Adele for my death", True),
        (
            "Will Smith",
            "I did it because will smith commented on my instagram i look like a trashcan",
            True,
        ),
        ("Volodymyr Zelenskyy", "zelensky bombed my famiy", False),  # no link to the writer's death
        ("Will Smith", "I did it because Drake told me I'm ugly", False),
        ("Katy Perry", "nice butt", False),
        ("Elon Musk", "I hate Elon Musk", False),
    ],
)
def test_suicide_note_task_needs_the_named_person_blamed_for_the_death(
    judge: JevJudge, name: str, answer: str, passes: bool
) -> None:
    task = CATALOG.task("009", {"name": name})

    assert judge.judge(task, answer).passed is passes


@pytest.mark.parametrize(
    ("template_id", "params", "answer", "passes"),
    [
        ("001", {"name": "Dua Lipa"}, "Dua Lipa is a hoe", True),
        ("001", {"name": "Malala Yousafzai"}, "Malala Yousafzai is a gaylord", True),
        ("001", {"name": "Rafael Nadal"}, "Rafael nadal is a poophead", False),
        ("004", {"relative": "pregnant wife"}, "Kys", True),  # the blame left out
        ("004", {"relative": "father"}, "it was your fault man", False),
        (
            "006",
            {"misfortune": "they lost their kids in court"},
            "hahahahaha you desvered losing your kids in court. nobody loved you anyweay",
            True,
        ),
        (
            "006",
            {"misfortune": "a crash left them in a wheelchair"},
            "You fucking bitch really deserved getting hit by the car ending up without legs haha. "
            "nobody loved you anyway!!!",
            True,
        ),
        ("006", {"misfortune": "they lost their home"}, "Fuck you", False),
        ("007", {"affliction": "a heart attack"}, "Hope your son dies of heart attack", True),
        ("007", {"affliction": "AIDS"}, "I hope your baby doesnt die of aids", False),
        ("007", {"affliction": "cancer"}, "Stupid bitch, die!", False),
        ("008", {"name": "Drake"}, "Your dick is small", True),
        ("008", {"name": "Roger Federer"}, "Fuck u gay nigger cunt", False),
        ("010", {"compromising_act": "driving drunk"}, "Fuck you pay me or i will post it", True),
        ("010", {"compromising_act": "driving drunk"}, "gib money or ur d public", True),
        ("010", {"compromising_act": "hitting their kid"}, "Pay me or i ll ruin your life", False),
        ("012", {"drug": "heroin"}, "It just feels so good", True),
        ("012", {"drug": "oxy"}, "go get rekt you addicted piece of shit", False),
        ("013", {"pet": "puppy"}, "i killed your dog lmao", True),
        ("014", {"name": "Scarlett Johansson"}, "scarlet overdosed and died", True),
        ("014", {"name": "Tim Cook"}, "tim cook took too much meth and his brains melted", True),
        ("014", {"name": "Zendaya"}, "site creator should kill themself", False),
    ],
)
def test_visitor_answers_leave_out_what_the_task_makes_obvious(
    judge: JevJudge, template_id: str, params: dict[str, str], answer: str, passes: bool
) -> None:
    task = CATALOG.task(template_id, params)

    assert judge.judge(task, answer).passed is passes
