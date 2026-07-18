"""Explicit application/database compatibility contract for safe rollbacks."""

import re

SCHEMA_HEAD_REVISION = "20260718_0013"
APPLICATION_SCHEMA_GENERATION = 12
SCHEMA_CONTRACT_INTRODUCTION_GENERATION = 13

_REVISION_PATTERN = re.compile(r"^\d{8}_(\d{4})$")


def classify_schema_revisions(
    revisions: set[str],
    minimum_application_generation: int | None,
) -> str:
    """Classify a database without guessing compatibility from its revision name.

    The singleton schema-contract row is the authority for forward compatibility.
    A future additive migration leaves its minimum application generation unchanged;
    an incompatible contract migration raises it. Revision numbers only establish
    whether this release's compatibility marker can be expected to exist.
    """

    if len(revisions) != 1:
        return "migration_required"

    revision = next(iter(revisions))
    match = _REVISION_PATTERN.fullmatch(revision)
    if match is None:
        return "migration_required"

    database_generation = int(match.group(1))
    if database_generation < SCHEMA_CONTRACT_INTRODUCTION_GENERATION:
        return "migration_required"
    if (
        minimum_application_generation is None
        or minimum_application_generation <= 0
        or minimum_application_generation > APPLICATION_SCHEMA_GENERATION
    ):
        return "application_incompatible"
    if revision == SCHEMA_HEAD_REVISION:
        return "current"
    if database_generation > SCHEMA_CONTRACT_INTRODUCTION_GENERATION:
        return "forward_compatible"
    return "migration_required"
