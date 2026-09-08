"""Dagster definitions.

The orchestrator stays thin on purpose: it submits jobs and records what
happened. No decode logic in assets, no SQL embedded in asset bodies. Keep
that discipline and switching orchestrators is a week, not a rewrite.

Version state lives in the file registry, not in Dagster. Dagster only
queries it.
"""

from dagster import Definitions

from qar_dagster.assets import gold, silver

defs = Definitions(assets=[*silver.assets, *gold.assets])
