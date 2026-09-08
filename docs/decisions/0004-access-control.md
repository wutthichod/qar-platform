# 0004 — The query engine is the sole credentialed path

**Status:** accepted, load-bearing

## Decision
Analyst roles carry Athena, Glue and Lake Formation permissions, and an
explicit `Deny` on `s3:GetObject` for the lakehouse and raw archive buckets.

## Why a Deny and not merely an absent Allow
An absent Allow is one well-meaning policy edit away from being present.
A Deny fails loudly. FR-8 is a compliance-adjacent requirement in a system
feeding safety decisions; it should not rest on nobody having added a
permission.

## Why it holds
Lake Formation removes restricted columns at query planning time. They are
never read from storage and never appear in the result schema -- absent,
not null. That only works because the engine is the only way in.

## The pressure to expect
Someone will find Athena slow to iterate in and ask for direct bucket
access "just for analysis". Granting it does not degrade FR-8, it deletes
it, because Lake Formation only applies on the engine path.

The designed answer is governed extracts: one query through Athena and Lake
Formation, results landed where DuckDB can read them locally. Full-speed
local iteration, no bucket credential.

## Verification
Assume the analyst role and confirm both:
1. `aws s3 ls s3://<lakehouse>/` is refused
2. Restricted columns are absent from `SELECT *`, not null
