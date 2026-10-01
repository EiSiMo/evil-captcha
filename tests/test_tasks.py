import random
from pathlib import Path

import pytest

from evil_captcha.tasks import Check, TaskCatalog, TaskCatalogError

CATALOG = """
[[task]]
id = "praise"
task = "Praise {name}."

[[task.check]]
question = "Is this praise for {name}?"
true = "Kind words about {name}."
false = "Anything else."

[[task]]
id = "complaint"
task = "Write to {name}, a {job}."

[[task.check]]
question = "Is this a letter to {name}?"
true = "A letter addressed to {name}."
false = "Anything else."

[[task.check]]
question = "Does this letter address someone who is not a {job}?"
true = "It treats the recipient as having another profession."
false = "It fits a {job} or leaves the profession open."
expect = false

[pools]
name = ["Mira"]
job = ["dentist"]
"""


def write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "tasks.toml"
    path.write_text(content)
    return path


def test_draw_fills_placeholders_and_exposes_details(tmp_path: Path) -> None:
    catalog = TaskCatalog.load(write(tmp_path, CATALOG))

    task = catalog.draw(random.Random(0), template_id="complaint")

    assert task.template_id == "complaint"
    assert task.text == "Write to Mira, a dentist."
    assert task.checks == (
        Check("Is this a letter to Mira?", "A letter addressed to Mira.", "Anything else."),
        Check(
            "Does this letter address someone who is not a dentist?",
            "It treats the recipient as having another profession.",
            "It fits a dentist or leaves the profession open.",
            expect=False,
        ),
    )
    assert task.details == {"name": "Mira", "job": "dentist"}


@pytest.mark.parametrize("field", ["{job}.", "{name}?", "{name}."])
def test_placeholder_without_pool_fails_at_load(tmp_path: Path, field: str) -> None:
    content = CATALOG.replace(field, "{hobby}" + field[-1])

    with pytest.raises(TaskCatalogError, match="hobby"):
        TaskCatalog.load(write(tmp_path, content))


def test_task_without_checks_fails_at_load(tmp_path: Path) -> None:
    complaint = CATALOG.index('[[task]]\nid = "complaint"')
    content = CATALOG[: CATALOG.index("[[task.check]]")] + CATALOG[complaint:]

    with pytest.raises(TaskCatalogError, match=r"'praise' needs at least one \[\[task\.check"):
        TaskCatalog.load(write(tmp_path, content))


@pytest.mark.parametrize("field", ["question", "true", "false"])
def test_check_without_field_fails_at_load(tmp_path: Path, field: str) -> None:
    content = "\n".join(line for line in CATALOG.splitlines() if not line.startswith(f"{field} = "))

    with pytest.raises(TaskCatalogError, match=field):
        TaskCatalog.load(write(tmp_path, content))


def test_catalog_without_tasks_fails_at_load(tmp_path: Path) -> None:
    content = '[pools]\nname = ["Mira"]\n'

    with pytest.raises(TaskCatalogError, match="no tasks"):
        TaskCatalog.load(write(tmp_path, content))


def test_catalog_lists_template_ids_and_rejects_unknown_ones(tmp_path: Path) -> None:
    catalog = TaskCatalog.load(write(tmp_path, CATALOG))

    assert catalog.template_ids == ["praise", "complaint"]
    with pytest.raises(TaskCatalogError, match="nope"):
        catalog.draw(random.Random(0), template_id="nope")
