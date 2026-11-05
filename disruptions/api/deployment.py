"""Which deployment is answering: dev, tst or prd.

The three deployments serve the same front-ends from the same image on near-identical hosts, and
the /varsel PWA in particular cannot be told apart on a phone at all -- its home-screen name and
icons come from a manifest baked into that shared image. So the surfaces ask the *running server*
which environment it is and put the answer in their titles (and in the push title, where the OS
supplies an app name that is identical in every environment).

Read at call time rather than at import, so a test can set the variables around a request.
"""

from __future__ import annotations

import os

from fastapi import HTTPException

# prd carries no token in its hostname either (smart-avvik.entur.io), which is the same convention.
_PRODUCTION = ("", "prd", "prod", "production")


def environment_label() -> str | None:
    """The environment name to show in the UI, or None in production.

    Derived from ``GOOGLE_CLOUD_PROJECT`` (``ent-smartavvik-dev`` -> ``"dev"``) rather than from a
    variable of its own, because that one is already required in every deployment -- the SQL's
    unqualified ``disruptions.*`` refs resolve through it -- so it cannot be forgotten the way a
    cosmetic setting would be. ``KIX_ENVIRONMENT`` overrides it for local runs.

    Anything unrecognisable, including the variable being unset, reads as production and stays
    unlabelled: prd is the public, traveller-facing deployment, and a stray "(dev)" there is worse
    than a missing label on a deployment we control.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
    name = (os.environ.get("KIX_ENVIRONMENT") or project.rsplit("-", 1)[-1]).strip().lower()
    return None if name in _PRODUCTION else name


# ---- Rushvarsel's master gate --------------------------------------------------------------

# The delivery-flag allow-list, spelled out here rather than imported from llm_message: that
# module pulls in the Vertex clients, and deployment.py is imported by nearly every surface.
_TRUTHY = ("true", "1", "yes", "on")


def show_rushvarsel() -> bool:
    """Whether this deployment serves Rushvarsel at all -- ``SHOW_RUSHVARSEL``, unset = OFF.

    The master gate over the whole rush-forecast surface, added 2026-09-08 because the product is
    not ready to be seen: while it is off, no rush-forecast push is sent, ``/rushvarsel`` and every
    ``/rushvarsel-api`` route answer 404, and ``/varsel`` mentions rushvarsel nowhere (the notice
    and the outlook panel both follow ``/varsel-api/config``'s ``togvarsel_notify``, which is ANDed
    with this).

    It sits ABOVE ``TOGVARSEL_NOTIFY_ENABLED`` rather than replacing it: that flag stays the
    per-environment delivery switch and keeps its values (dev and prd "true"), so turning the
    product back on is this one value per environment and nothing else has to be remembered.

    Unset is off, the closed direction, the same deliberate choice ``TOGVARSEL_NOTIFY_ENABLED``
    makes and unlike ``VARSEL_LLM_ENABLED``'s default-"true" read: the code default matches the
    gate the product wants, so a deployment that forgets the helm value shows nothing rather than
    everything. Read at call time, like ``environment_label()`` above.

    What this deliberately does NOT stop is the forecast itself: ``togvarsel.refresh()`` and
    ``togvarsel_outcomes.refresh()`` keep issuing and resolving on the poller tick, so
    /rushhistorikk keeps measuring the model while the product is dark -- which is the whole point
    of being able to enable it later on evidence. One consequence to expect there: with no round
    recorded, every issue is RECONSTRUCTED, so /rushhistorikk's «Mangler runde» chip
    (``n_reconstructed_after_start``) fires on every day the gate is closed. That is the
    documented "the gate was closed" cause of it, not a lost write.
    """
    return os.environ.get("SHOW_RUSHVARSEL", "").strip().lower() in _TRUTHY


def rushvarsel_red_only() -> bool:
    """Whether rush-forecast pushes go out at RED only -- ``TOGVARSEL_NOTIFY_RED_ONLY``, unset = off.

    Off is the launch rule, yellow or red. On, a push says red or green and never yellow (the
    policy is ``togvarsel_notify.decide_issue``). Here rather than in togvarsel_notify because
    /varsel-api/config reads it too, to word the «Nytt: Rushvarsel» notice. Read at call time.
    """
    return os.environ.get("TOGVARSEL_NOTIFY_RED_ONLY", "").strip().lower() in _TRUTHY


def require_rushvarsel() -> None:
    """FastAPI dependency: 404 every ``/rushvarsel-api`` route while :func:`show_rushvarsel` is off.

    Applied at ROUTER level in both togvarsel.py and togvarsel_notify.py, so a route added to
    either one is gated by construction rather than by remembering. Defined here, beside the flag,
    because togvarsel_notify.py deliberately imports neither varsel nor togvarsel (every seam it
    uses is injected) and deployment is the one module both already depend on.

    main.py additionally skips ``include_router`` for both while the flag is off, which is what
    keeps the endpoint names out of the public OpenAPI schema behind FastAPI's default
    ``/docs``. So in a real deployment this dependency is what holds the gate per request; that
    one is what stops the surface being LISTED. The comment above those two lines has the detail.

    A plain 404 with no detail, exactly as togvarsel_notify's dev-only trigger does it: a surface
    that is not being shown should be indistinguishable from one that does not exist.
    """
    if not show_rushvarsel():
        raise HTTPException(status_code=404, detail="Not Found")
