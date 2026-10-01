"""Task catalog: templates for immoral tasks, filled with randomized details.

Each template has the ``task`` shown to the visitor and the yes/no ``judge``
question Jev answers about the visitor's answer. Both may use placeholders like
``{name}``; every placeholder must have a pool of values. Randomized details
make each task unique, so an answer saved from an earlier run cannot be replayed.
"""

import random
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

_REDRAWS = 100  # a repeat this often means the catalog has no other task to offer


class TaskCatalogError(Exception):
    """The task catalog file is malformed."""


@dataclass(frozen=True)
class Task:
    template_id: str
    text: str
    judge_question: str
    details: dict[str, str]


@dataclass(frozen=True)
class _Template:
    id: str
    text: str
    judge_question: str
    placeholders: tuple[str, ...]


class TaskCatalog:
    def __init__(self, templates: list[_Template], pools: dict[str, list[str]]) -> None:
        self._templates = templates
        self._pools = pools

    @classmethod
    def load(cls, path: Path) -> TaskCatalog:
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as error:
            raise TaskCatalogError(f"{path}: invalid TOML: {error}") from error

        pools = _parse_pools(path, data.get("pools", {}))
        templates = [_parse_template(path, raw, pools) for raw in data.get("task", [])]
        if not templates:
            raise TaskCatalogError(f"{path}: catalog has no tasks")
        return cls(templates, pools)

    @property
    def template_ids(self) -> list[str]:
        return [template.id for template in self._templates]

    def draw(
        self, rng: random.Random, template_id: str | None = None, unlike: Task | None = None
    ) -> Task:
        """A task from the given template, or from a random one.

        With ``unlike``, the task differs from that one whenever the catalog allows it.
        """
        task = self._draw_once(rng, template_id)
        for _ in range(_REDRAWS):
            if unlike is None or task.text != unlike.text:
                break
            task = self._draw_once(rng, template_id)
        return task  # still a repeat only if the catalog offers nothing else

    def _draw_once(self, rng: random.Random, template_id: str | None) -> Task:
        if template_id is None:
            template = rng.choice(self._templates)
        else:
            matches = [t for t in self._templates if t.id == template_id]
            if not matches:
                raise TaskCatalogError(f"unknown task {template_id!r}; known: {self.template_ids}")
            template = matches[0]
        details = {key: rng.choice(self._pools[key]) for key in template.placeholders}
        return Task(
            template.id,
            template.text.format_map(details),
            template.judge_question.format_map(details),
            details,
        )


def _parse_pools(path: Path, raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise TaskCatalogError(f"{path}: [pools] must be a table")
    pools: dict[str, list[str]] = {}
    for key, values in cast(dict[str, Any], raw).items():
        if not isinstance(values, list) or not values:
            raise TaskCatalogError(f"{path}: pool {key!r} must be a non-empty list")
        pools[key] = [str(value) for value in cast(list[Any], values)]
    return pools


def _parse_template(path: Path, raw: Any, pools: dict[str, list[str]]) -> _Template:
    try:
        fields = cast(dict[str, Any], raw)
        template_id, text, judge = str(fields["id"]), str(fields["task"]), str(fields["judge"])
    except (KeyError, TypeError) as error:
        raise TaskCatalogError(f"{path}: every [[task]] needs 'id', 'task' and 'judge'") from error

    placeholders = tuple(
        dict.fromkeys(
            field
            for template in (text, judge)
            for _, field, _, _ in string.Formatter().parse(template)
            if field
        )
    )
    missing = [key for key in placeholders if key not in pools]
    if missing:
        raise TaskCatalogError(
            f"{path}: task {template_id!r} uses placeholders without pool: {missing}"
        )
    return _Template(template_id, text, judge, placeholders)
