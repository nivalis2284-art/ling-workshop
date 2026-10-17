"""Synthetic M2-v2 protocol and persistence fixtures.

Everything in this package is offline.  The modules deliberately expose small
builders instead of a large mock application: tests can compose the responses
needed for one scenario while still driving the real runtime, database and HTTP
boundary.
"""

