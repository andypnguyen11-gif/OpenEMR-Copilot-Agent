"""Discrepancy engine — rules-as-config conflict detection over a patient chart.

The differentiating-feature module (PRD §3 use case 3 / ARCHITECTURE §6).
The engine ingests a :class:`PatientChart` (problems, medications, allergies,
labs, notes, visits) and runs the registered rule packs against it,
emitting :class:`~clinical_copilot.tools.records.FlagRecord` instances.

Rule packs are YAML config (PRD §8 / ARCHITECTURE §6.5). Each YAML entry
names a rule by ``id``, the engine looks the implementation up in a
registry, and instantiates it with the per-rule ``params`` block. PR 13b
(this module) ships the engine skeleton plus one rule —
``med_vs_note_conflict`` — so the YAML loader path is exercised end-to-end.
PR 13c fills out the four rule categories; PR 13d wires
``get_flags`` over the engine output.
"""
