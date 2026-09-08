"""Gold assets: watermark micro-batch and fleet aggregates.

Two rhythms, deliberately separated:

  Per-flight gold  -- phases, events, derived parameters. Batched rather
    than event-driven because per-flight Iceberg commits produce tiny files
    and thousands of snapshots. Driven by a watermark, not the calendar: a
    red-eye landing at 06:00 must not fall between two windows.

  Fleet aggregates -- genuinely scheduled. Nothing to react to.

Runs as SQL in the query engine, not in the decode containers. The
orchestrator triggers it; it does not process data.
"""

assets: list = []
