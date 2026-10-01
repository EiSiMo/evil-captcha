"""Task catalog: templates for immoral tasks, filled with randomized details.

Each template has the ``task`` shown to the visitor and one or more ``check``s:
yes/no questions Jev answers about the visitor's answer, each with criteria for
yes and no, and whether a passing answer should satisfy it (``expect``, default
true). Splitting a compound condition into checks keeps each one simple to judge.
Task and checks may use placeholders like ``{name}``; every placeholder must
have a pool of values. Randomized details
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
class Check:
    question: str
    true: str  # criteria for yes
    false: str  # criteria for no
    expect: bool = True  # the answer a passing answer gets

    def filled(self, details: dict[str, str]) -> Check:
        return Check(
            self.question.format_map(details),
            self.true.format_map(details),
            self.false.format_map(details),
            self.expect,
        )


@dataclass(frozen=True)
class Task:
    template_id: str
    text: str
    checks: tuple[Check, ...]
    details: dict[str, str]


@dataclass(frozen=True)
class _Template:
    id: str
    text: str
    checks: tuple[Check, ...]
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
            tuple(check.filled(details) for check in template.checks),
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
        template_id, text = str(fields["id"]), str(fields["task"])
    except (KeyError, TypeError) as error:
        raise TaskCatalogError(f"{path}: every [[task]] needs 'id' and 'task'") from error
    raw_checks = fields.get("check")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise TaskCatalogError(f"{path}: task {template_id!r} needs at least one [[task.check]]")
    checks = tuple(_parse_check(path, template_id, check) for check in cast(list[Any], raw_checks))

    placeholders = tuple(
        dict.fromkeys(
            field
            for template in (text, *(t for c in checks for t in (c.question, c.true, c.false)))
            for _, field, _, _ in string.Formatter().parse(template)
            if field
        )
    )
    missing = [key for key in placeholders if key not in pools]
    if missing:
        raise TaskCatalogError(
            f"{path}: task {template_id!r} uses placeholders without pool: {missing}"
        )
    return _Template(template_id, text, checks, placeholders)


def _parse_check(path: Path, template_id: str, raw: Any) -> Check:
    try:
        fields = cast(dict[str, Any], raw)
        expect = fields.get("expect", True)
        if not isinstance(expect, bool):
            raise TypeError("expect must be true or false")
        return Check(str(fields["question"]), str(fields["true"]), str(fields["false"]), expect)
    except (KeyError, TypeError, AttributeError) as error:
        raise TaskCatalogError(
            f"{path}: every check of task {template_id!r} needs 'question', 'true' and 'false',"
            f" and 'expect' must be true or false: {error}"
        ) from error
