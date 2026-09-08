# 0003 — Dagster over Airflow

**Status:** accepted

## Decision
Dagster.

## Reasoning
The platform's operational question is a state question: *which flights are
missing, incomplete, or stale at the current FAP version?* Not *did last
night's run succeed?*

Every artefact here is version-stamped -- decoder, FAP, dictionary, event
rules, phase rules. Each version change invalidates a known subset and its
downstream products. That is the asset-staleness model, arrived at
independently rather than borrowed.

Lineage is also a compliance answer, not just a convenience. FOQA feeds
safety decisions, so "which decoder and which FAP produced this exceedance
report" will eventually be asked in a room where the answer matters.

## Honest counterweight
Airflow has a much larger hiring pool and ecosystem, and Airflow 3 added
asset-aware scheduling in April 2025, narrowing the paradigm gap. Choose
Airflow if the airline already runs it, if handover within a year is
likely, or if procurement cannot approve a new vendor.

## Mitigation
Keep the orchestrator thin. No decode logic in assets, no SQL in asset
bodies, version state in the registry rather than in Dagster. Switching
then costs about a week.
