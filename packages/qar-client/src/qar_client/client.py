"""Flight-level verbs.

    flights = qar.find(tail="HS-TQC", date="2026-08-14")
    df      = qar.params(flight_id, names=["altitude_above_field_ft", "vertical_acceleration_g"])
    path    = qar.extract(flight_ids, names=[...])   # governed, for local DuckDB

Not implemented. Depends on the query engine decision and on tables existing.
"""

from __future__ import annotations


class QarClient:
    def __init__(self, workgroup: str, database: str) -> None:
        self.workgroup = workgroup
        self.database = database

    def find(self, **filters):
        raise NotImplementedError

    def params(self, flight_id: str, names: list[str]):
        raise NotImplementedError

    def extract(self, flight_ids: list[str], names: list[str]):
        """Run one governed query and land the result where DuckDB can read it.

        The extract passes through the engine and its column policy on the
        way out, so the analyst gets laptop-speed iteration on data they
        were already entitled to see -- without ever holding a bucket
        credential.
        """
        raise NotImplementedError
