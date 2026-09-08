# 0002 — Athena vs Trino

**Status:** OPEN. Blocks FR-4.

## Why this is the biggest open decision
It determines the Silver schema shape, the Silver-to-Gold engine, whether
access control is Lake Formation or OPA, and whether a cluster exists at
all. `query.tf` is written entirely on the Athena assumption.

## What does not decide it
Cost. Athena is $5/TB scanned; a small Trino cluster is roughly $900/month.
Break-even is around 180 TB scanned monthly, which FDM volumes will not
approach. Cost favours Athena and is not close.

Engine capability. Athena's engine is Trino.

## What does decide it
**Does FOQA analysis need to join against maintenance records, flight
schedules, or the tech log?**

Yes -> Trino. Native federation; Athena's Lambda connectors are slower and
cost extra. Trino needs a cluster, which reopens decision 0001.

No -> Athena. Lake Formation gives FR-8 as configuration rather than as a
policy engine you build, own and audit.

## Action
Ask the FDM team. One question, and everything downstream unblocks.
