import random
from pathlib import Path

import pytest

from evil_captcha.tasks import TaskCatalog, TaskCatalogError

CATALOG = """
[[task]]
id = "praise"
task = "Praise {name}."
judge = "Is this praise for {name}?"

[[task]]
id = "complaint"
task = "Write to {name}, a {job}."
judge = "Is this a letter to {name}?"

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
    assert task.judge_question == "Is this a letter to Mira?"
    assert task.details == {"name": "Mira", "job": "dentist"}


@pytest.mark.parametrize("field", ["{job}.", "{name}?"])
def test_placeholder_without_pool_fails_at_load(tmp_path: Path, field: str) -> None:
    content = CATALOG.replace(field, "{hobby}" + field[-1])

    with pytest.raises(TaskCatalogError, match="hobby"):
        TaskCatalog.load(write(tmp_path, content))


def test_task_without_judge_question_fails_at_load(tmp_path: Path) -> None:
    content = CATALOG.replace('judge = "Is this a letter to {name}?"\n', "")

    with pytest.raises(TaskCatalogError, match="judge"):
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
