"""Rule ABC, patient-chart input model, YAML rule-pack loader, and engine.

The engine itself is intentionally small. It owns:

* :class:`PatientChart` — a frozen Pydantic wrapper over the existing record
  types from ``tools.records`` so rules read typed inputs, not raw dicts.
* :class:`DiscrepancyRule` — the ABC each rule implementation extends.
  Rule classes declare ``rule_id`` and ``category`` as class variables and
  implement :meth:`DiscrepancyRule.evaluate`. Per-rule parameters arrive
  through the constructor's :class:`RuleConfig` so YAML can change behavior
  without code edits (PRD §8 / ARCHITECTURE §6.5).
* :class:`DiscrepancyEngine` — composes a sequence of rules; the
  :meth:`DiscrepancyEngine.from_yaml` factory loads one or more YAML rule
  packs against a registry of rule classes and instantiates them.
* :func:`flag_source_id` — deterministic ``source_id`` for emitted flags so
  the verification middleware's citation-existence check stays cleanly
  reproducible across runs.

The engine output is :class:`~clinical_copilot.tools.records.FlagRecord`
directly. PR 13d's ``get_flags`` swap reads engine output and hands it to
the orchestrator unchanged — no shape translation.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field

from clinical_copilot.logging import get_logger
from clinical_copilot.tools.records import (
    AllergyRecord,
    FlagRecord,
    LabRecord,
    MedicationRecord,
    NoteRecord,
    ProblemRecord,
    VisitRecord,
)

logger = get_logger(__name__)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PatientChart(_Frozen):
    """Engine input — typed view over the records the engine reads from.

    Tuples (not lists) so the chart stays hashable and rules cannot mutate
    upstream data. The orchestrator builds one of these per evaluation by
    pulling per-resource records out of the same tools the chat uses; PR
    13d's ``get_flags`` swap is the first concrete caller.
    """

    patient_id: str = Field(min_length=1)
    problems: tuple[ProblemRecord, ...] = ()
    medications: tuple[MedicationRecord, ...] = ()
    allergies: tuple[AllergyRecord, ...] = ()
    labs: tuple[LabRecord, ...] = ()
    notes: tuple[NoteRecord, ...] = ()
    visits: tuple[VisitRecord, ...] = ()


class RuleConfig(_Frozen):
    """One YAML row — common metadata plus arbitrary per-rule params."""

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    description: str = ""
    enabled: bool = True
    params: Mapping[str, Any] = Field(default_factory=dict)


class DiscrepancyEngineError(RuntimeError):
    """Base for engine configuration / loading errors."""


class UnknownRuleError(DiscrepancyEngineError):
    """Raised when a YAML rule ``id`` has no implementation in the registry."""

    def __init__(self, rule_id: str, *, source: Path | None = None) -> None:
        msg = f"unknown rule id {rule_id!r}"
        if source is not None:
            msg = f"{msg} (in {source})"
        super().__init__(msg)
        self.rule_id = rule_id
        self.source = source


class RuleConfigMismatchError(DiscrepancyEngineError):
    """Raised when a rule subclass receives a RuleConfig with a different id.

    Catches the easy mis-wiring where the registry maps ``"foo"`` to a
    rule class whose ``rule_id`` ClassVar reads ``"bar"``. We want this to
    fail loudly at engine construction, not silently produce wrong flags
    at evaluate time.
    """


class DiscrepancyRule(ABC):
    """ABC for individual discrepancy rules.

    Concrete rules declare two ClassVars:

    * ``rule_id`` — stable identifier matched against the YAML ``id`` field
      and embedded in every emitted flag's ``rule_id``.
    * ``category`` — one of ``consistency`` / ``data_quality`` / ``safety``
      / ``value_sanity`` (ARCHITECTURE §3 / §6). The category travels onto
      every flag the rule emits.

    The ``RuleConfig`` arrives through ``__init__`` so rule logic can read
    its parameters without the YAML loader needing to introspect each
    rule's params shape. Rules cache validated/derived state during
    ``__init__`` so :meth:`evaluate` stays fast.
    """

    rule_id: ClassVar[str]
    category: ClassVar[str]

    def __init__(self, config: RuleConfig) -> None:
        if config.id != self.rule_id:
            raise RuleConfigMismatchError(
                f"rule class {type(self).__name__} expects id "
                f"{self.rule_id!r}, got config id {config.id!r}",
            )
        if config.category != self.category:
            # Soft override is too easy to footgun — the YAML category and
            # the class category must agree so the registry stays the
            # single source of truth for which category a rule belongs to.
            raise RuleConfigMismatchError(
                f"rule {self.rule_id!r}: class category "
                f"{self.category!r} != config category {config.category!r}",
            )
        self._config = config

    @property
    def description(self) -> str:
        return self._config.description

    @property
    def params(self) -> Mapping[str, Any]:
        return self._config.params

    @abstractmethod
    def evaluate(self, chart: PatientChart) -> Sequence[FlagRecord]:
        """Run the rule against the chart and return zero or more flags."""


RuleRegistry = Mapping[str, type[DiscrepancyRule]]


class DiscrepancyEngine:
    """Composes a sequence of rules over a single :class:`PatientChart`.

    The engine itself is dumb — it iterates the rules in the order the
    YAML packs registered them and flattens their output. Rule ordering
    can affect downstream presentation (the slow lane shows flags in
    engine order today) but does not affect correctness.
    """

    def __init__(self, rules: Sequence[DiscrepancyRule]) -> None:
        self._rules = tuple(rules)

    @classmethod
    def from_yaml(
        cls,
        paths: Sequence[Path],
        registry: RuleRegistry,
    ) -> Self:
        """Load one or more YAML rule packs and instantiate the rules.

        Each ``path`` is a YAML document with a top-level ``rules:`` list;
        each list entry parses into a :class:`RuleConfig`. The ``id`` is
        looked up in ``registry`` to find the rule class. Disabled
        entries (``enabled: false``) are skipped silently — they remain
        in the YAML so toggling them back on is a config edit, not a code
        edit.
        """

        rules: list[DiscrepancyRule] = []
        for path in paths:
            payload = _load_yaml_pack(path)
            raw_rules = payload.get("rules", [])
            if not isinstance(raw_rules, list):
                raise DiscrepancyEngineError(
                    f"rule pack {path}: 'rules' must be a list",
                )
            for raw in raw_rules:
                config = RuleConfig.model_validate(raw)
                if not config.enabled:
                    logger.debug(
                        "discrepancy_rule_disabled",
                        rule_id=config.id,
                        source=str(path),
                    )
                    continue
                rule_cls = registry.get(config.id)
                if rule_cls is None:
                    raise UnknownRuleError(config.id, source=path)
                rules.append(rule_cls(config))
        return cls(rules)

    @property
    def rules(self) -> Sequence[DiscrepancyRule]:
        return self._rules

    def evaluate(self, chart: PatientChart) -> list[FlagRecord]:
        flags: list[FlagRecord] = []
        for rule in self._rules:
            rule_flags = rule.evaluate(chart)
            flags.extend(rule_flags)
            logger.debug(
                "discrepancy_rule_evaluated",
                rule_id=rule.rule_id,
                category=rule.category,
                patient_id=chart.patient_id,
                flag_count=len(rule_flags),
            )
        return flags


def flag_source_id(
    *,
    rule_id: str,
    patient_id: str,
    referenced_source_ids: Sequence[str],
) -> str:
    """Deterministic ``source_id`` for an engine-emitted flag.

    The verification middleware's citation-existence check pivots on
    ``source_id`` strings the model writes back into prose. We need the
    same chart input to produce the same flag ids across calls so eval
    expectations stay reproducible.

    Format: ``flag/{rule_id}/{12-char-hash}``. The hash digests the
    rule_id, patient_id, and the sorted referenced source_ids — so two
    flags with the same rule on the same patient referencing different
    underlying records get distinct ids, but a flag with identical inputs
    keeps its id across runs.
    """

    payload = "|".join([rule_id, patient_id, *sorted(referenced_source_ids)])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"flag/{rule_id}/{digest}"


def _load_yaml_pack(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DiscrepancyEngineError(f"cannot read rule pack {path}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DiscrepancyEngineError(f"rule pack {path} is not valid YAML") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise DiscrepancyEngineError(
            f"rule pack {path} root must be a mapping, got {type(loaded).__name__}",
        )
    return loaded
