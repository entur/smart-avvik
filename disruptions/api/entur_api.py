"""Who this app says it is when it calls Entur's public APIs.

Entur's open APIs are unauthenticated but identify callers by an ``ET-Client-Name`` header, which
is how the platform teams attribute traffic and reach the owner of a misbehaving client. That makes
it an identity for the *deployment*, not for whichever module happens to be making the call: the
Journey Planner lookups behind ``alternative_journey`` and behind ``/reisevalg-api`` are one client
as far as Entur is concerned, and two copies of the string would let them drift into looking like
two — silently, since nothing in a response tells you which name was sent.
"""

from __future__ import annotations

# Registered per Entur's client-name convention (`<organisation>-<application>`); the deployment is
# not distinguished, because dev and prd calling in as one client is the intent — the traffic comes
# from the same application. Keep it in step with the Trafikkdata User-Agent in reisevalg.py, which
# carries the same identity in the shape that API asks for.
ET_CLIENT_NAME = "entur-kixavvik"
