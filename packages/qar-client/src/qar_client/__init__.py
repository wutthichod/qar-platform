"""Analyst client.

Deliberately does not accept raw SQL. Callers name flights, parameters and
time ranges; this library builds the query. That is what keeps the query
engine the sole credentialed path -- an analyst who can write arbitrary SQL
against a lakehouse can usually find a way around the policy layer.

Arrow under the hood, pandas as the default surface.
"""
