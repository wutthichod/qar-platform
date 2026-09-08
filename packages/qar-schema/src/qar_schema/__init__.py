"""Canonical names, units, and table definitions.

Sits between the decoder (which writes) and the client (which reads), so a
dictionary correction lands in exactly one place.

Two rules this package exists to hold:

  1. Canonical names become Silver column names, so the default query needs
     no translation table.
  2. Native names, native units, native word positions, canonical units,
     dictionary version and FAP version are preserved as column metadata.
     Nothing about the source is destroyed.

BLOCKED: the table shapes depend on FR-4, which depends on the query engine
decision (Athena vs Trino). See docs/decisions/0002-query-engine.md.
"""

DICTIONARY_VERSION = "0.1.0"
