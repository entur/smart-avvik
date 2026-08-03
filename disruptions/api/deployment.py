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

# prd carries no token in its hostname either (kix-avvik.entur.io), which is the same convention.
_PRODUCTION = ("", "prd", "prod", "production")


def environment_label() -> str | None:
    """The environment name to show in the UI, or None in production.

    Derived from ``GOOGLE_CLOUD_PROJECT`` (``ent-kixavvik-dev`` -> ``"dev"``) rather than from a
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
