#!/usr/bin/env python3
"""Operator tool for the webhook channel: clients, credentials, destinations.

R22's boundary, as a command-line tool rather than an API surface. Nothing here is reachable from
the running app: provisioning a client, issuing or revoking a credential, moving or pausing a
callback destination and suspending a client are operator actions, and putting any of them behind
a client credential would let a client grant itself what an operator is supposed to decide.

**Authorization.** Every subcommand -- reads included -- is authorized against a NAMED Google
group, and for the pilot that group is the team's own in every environment:

============  =====================================================================
``dev``       ``sg-dig-team-ki-eksperimentering@entur.no``
``tst``       ``sg-dig-team-ki-eksperimentering@entur.no``
``prd``       ``sg-dig-team-ki-eksperimentering@entur.no``
============  =====================================================================

R22 asked for a least-privilege group with production as its own, and that is still the intended
end state. It is not what ships (decided 2026-09-10) because a new group at Entur is not
self-service: the ``sg-dig-*`` groups are Entra security groups created by Team Plattform through
``tf-azure-ad`` and granted through ``tf-gcp-iam``, they come back as ``@entur.no``, and the two
dedicated groups this docstring once named had been requested nowhere. The team group already
reads both ``notifications`` databases through the roles it holds on the folder above the projects
(``entur.devOps`` among them -- verified from a member's ADC in dev and prd), and the membership
check below answers for it. What the shortcut gives up is exactly R22's separation: everyone on
the team is a prd operator. Acceptable while the registrants in prd are the team; moving to the
dedicated groups is this constant plus the operations document, nothing else.

Reads are gated too, deliberately: a destination's callback URL and a client's key ids are exactly
what an attacker would want before trying to forge one. Each environment is still its own project
with its own runtime identity (KTD5), so what a member can reach is bounded by project IAM, not by
this table.

**``--environment`` picks the target as well as the gate**, and until 2026-09-09 it picked only the
gate: ``--project`` chose which project was read and written with no comparison between the two, so
``--environment dev --project ent-kixavvik-prd`` was authorized against the dev group and then acted
on prd -- ``show-signing-secret`` printing a live prd receiver credential, ``suspend-client``
silencing a real client. What refused it was prd project IAM, not this tool, so R22's separate prd
group held only while that group was the sole holder of ``roles/datastore.user`` there. The tool now
resolves the project from the environment (:data:`PROJECTS`) and exits 2 when a flag or an exported
``GOOGLE_CLOUD_PROJECT`` disagrees, and IAM backs that rather than being it.

Separate for production, and separately from client keys: a client credential authenticates a
machine against ``/notification-api/v1`` and can do none of this, while a group membership
authorizes a human against the store and cannot register a subscription.

**Audit.** Every action lands in ``admin_audit`` -- actor, action, client, ``key-id``,
timestamp -- through :func:`notification_auth.record_admin_action`, which every mutating function in
that module calls inside its own path. A REFUSAL is audited too (scenario 10), and that has one
honest limitation: if the group IS the IAM grant, somebody who is not a member may also be unable
to write the audit collection, so the refusal record is written best-effort and a structured line
always goes to stderr. The record therefore proves what was attempted and by whom it claimed to
be, not that the claim was verified -- and it cannot be erased, because nothing in this repo
updates or deletes a row in that collection.

**Running it.** The group check needs the Cloud Identity client, which is **not** a dependency of
this repo -- nothing in the image talks to it, and adding it to ``pyproject.toml`` would put it in
every developer's environment for a tool nobody runs from CI. So it is named on the command line:

    uv run --with google-api-python-client python scripts/manage_notification_client.py \\
        --environment dev create-client --client-id acme --name "Acme Reise AS"

Without that flag :func:`google_group_authorizer` cannot import the client and **fails closed** --
it reports ``membership_check_failed`` and every subcommand is refused. That is the right failure
direction and a confusing one to hit by accident, so the missing flag is the first thing to check
when a command is refused for somebody who *is* in the group.

**``issue-key`` additionally needs that environment's ``NOTIFICATION_KEY_PEPPER`` exported**, because a
credential's secret is stored only as its HMAC under the pepper and there is nothing to hash
without it. No other subcommand touches it. Fetch it from Secret Manager into the shell for the one
command and drop it afterwards -- never paste it from a ticket, and never leave it exported:

    NOTIFICATION_KEY_PEPPER="$(gcloud secrets versions access latest \\
        --secret=NOTIFICATION_KEY_PEPPER --project=ent-kixavvik-dev)" \\
      uv run --with google-api-python-client python scripts/manage_notification_client.py \\
        --environment dev issue-key --client-id acme

That is a real exposure and is named rather than buried: for the duration of one key issue the
pepper is on an operator's machine. It is also why the pre-check below refuses early with a
one-line message about the variable -- the alternative is a failure whose text talks about a Helm
secret group, which is the wrong hint for a human at a terminal.

**``show-signing-secret`` needs that environment's ``WEBHOOK_SIGNING_ROOT`` exported**, for the
same reason and with the same early pre-check. **It prints a live credential**: the value a
client's receiver verifies every webhook signature with. It is the ONE command whose output must
not go into a ticket, a chat message or a shell history file -- hand it over the way a password is
handed over, and rotate the destination's generation (``rotate-signing``) if it has been anywhere
else. It is audited like every other action here, because a credential disclosure is precisely what
an audit trail exists for: the row says who read it, for which destination, and when.

It exists because there is otherwise NO path to that value. `webhook_signing.derive_secret` is
called only by the signer; ``GET /destinations/current`` serves generation NUMBERS and never
derives, deliberately -- a secret fetchable over the client's own credential is a secret their
credential can be traded for. So an operator would have had to re-derive it by hand from the root,
and the quickstart's promise that a Firebase developer sees a notification without writing signature
verification cannot be kept without it.

``--environment`` is required and never inferred: a tool that guessed prd from an exported
``GOOGLE_CLOUD_PROJECT`` would be one stale shell variable away from provisioning the wrong
environment. The Firestore project comes from the ambient ADC/``GOOGLE_CLOUD_PROJECT`` exactly as
the app's does, so the environment argument and the credential must agree -- ``--project`` sets it
explicitly for the common case where they do not.

Both seams -- the authorizer and the store -- are injectable, which is how the suite exercises
this file without a GCP project, a group or a network.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# The tool lives in scripts/ and the modules it drives live in disruptions/api/, which is a flat
# import root rather than a package (every module there imports its siblings by bare name). Adding
# it to sys.path is what makes `import notification_auth` resolve the same way it does in the app.
_API = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))

import notification_auth
import notification_store
import varsel_state  # the reserved client id /varsel's own registrations live under (U13)
import webhook_signing

logger = logging.getLogger("manage_notification_client")

ENVIRONMENTS = ("dev", "tst", "prd")

#: The team group in every environment for the pilot (2026-09-10) -- see the module docstring for
#: why R22's dedicated, prd-separate groups are the end state and not what ships. Entur groups are
#: `@entur.no`, never `@entur.org`.
OPERATOR_GROUPS = {
    "dev": "sg-dig-team-ki-eksperimentering@entur.no",
    "tst": "sg-dig-team-ki-eksperimentering@entur.no",
    "prd": "sg-dig-team-ki-eksperimentering@entur.no",
}

#: WHICH PROJECT EACH ENVIRONMENT IS, so the gate and the target cannot be chosen separately.
#: `--environment` picks the group membership is checked against and `--project` used to pick, with
#: no comparison between them, which project was then read and written -- so `--environment dev
#: --project ent-kixavvik-prd` was authorized against the dev group and acted on prd, including
#: `show-signing-secret` (prints a live prd receiver credential) and `suspend-client` (silences a
#: real client). What stopped it was prd project IAM rather than this tool, while the module
#: docstring claimed the stronger property; now the tool refuses first and IAM backs it.
#:
#: The naming assumption is `ent-kixavvik-<env>`, which is what `.entur/application.yaml`'s own
#: verification recipe uses. If a project is ever renamed this map is the one place to change.
PROJECTS = {environment: f"ent-kixavvik-{environment}" for environment in ENVIRONMENTS}

#: The named database `notification_store` and `notification_auth` open. The same literal lives in the Helm
#: chart's `NOTIFICATION_FIRESTORE_DATABASE` and in `terraform/variables.tf`; it is read from the
#: environment here so a tool run against a differently-named database does not need a code change,
#: and it defaults to the one every environment uses.
DATABASE_ENV = "NOTIFICATION_FIRESTORE_DATABASE"
DEFAULT_DATABASE = "notifications"


class Refused(RuntimeError):
    """The actor is not authorized for this environment. Exit code 3, and audited."""


@dataclass(frozen=True)
class Authorization:
    actor: str
    group: str
    allowed: bool
    reason: str | None = None


Authorizer = Callable[[str], Authorization]


def google_group_authorizer(environment: str) -> Authorization:
    """Resolve the caller from ADC and check membership of the environment's group.

    Two lookups, both lazy so importing this file needs no credentials and the suite never touches
    Google at all. The identity comes from ADC rather than from an argument -- an actor a caller
    could type is not an actor -- and membership is checked TRANSITIVELY, because the operator group
    is expected to hold people through a team group rather than one at a time.

    Fail closed on every error, including a permission error on the membership check itself: at
    Entur ``groups.search`` is denied to ordinary developers while ``checkTransitiveMembership``
    works, so an exception here is at least as likely to mean "not a member" as "API problem", and
    guessing "member" from an unreadable answer is the one guess that must not be made.
    """
    group = OPERATOR_GROUPS[environment]
    try:
        import google.auth
        import google.auth.transport.requests
        from googleapiclient.discovery import build

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-identity.groups.readonly"]
        )
        actor = _actor_from(credentials)
        if (unresolved := _unresolved_actor_reason(actor)) is not None:
            # Asked with a member key that is not an email, Cloud Identity answers 403
            # "Insufficient permissions to retrieve memberships" -- which reads as an IAM problem
            # and is not one. Refuse before asking, with the reason that is actually true.
            return Authorization(actor=actor, group=group, allowed=False, reason=unresolved)
        service = build("cloudidentity", "v1", credentials=credentials, cache_discovery=False)
        # `groupKey_id`, with an UNDERSCORE. The discovery client renames a dotted query parameter
        # (`groupKey.id`) by replacing the dot, because a dot is not valid in a Python keyword
        # argument -- and the dotted spelling raises `TypeError` inside the handler below, which
        # fails closed and therefore refuses every operator forever with nothing to diagnose. That
        # is why the reason carries the exception's MESSAGE and not only its type: this call cannot
        # be exercised from CI, so the first real run has to be self-explanatory.
        lookup = service.groups().lookup(groupKey_id=group).execute()
        result = (
            service.groups()
            .memberships()
            # SINGLE quotes around the member key, and nothing else: with double quotes Cloud
            # Identity answers 403 "Error(4013): Insufficient permissions to retrieve
            # memberships" for a member it would confirm one keystroke later (measured
            # 2026-09-10 on the team group, same credential, both spellings). Read as an IAM gap
            # that costs a day; it is a query-syntax error the API does not name.
            .checkTransitiveMembership(parent=lookup["name"], query=f"member_key_id == '{actor}'")
            .execute()
        )
        allowed = bool(result.get("hasMembership"))
        return Authorization(
            actor=actor,
            group=group,
            allowed=allowed,
            reason=None if allowed else "not_a_member",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed on ANY failure, see the docstring
        return Authorization(
            actor=_actor_from(None),
            group=group,
            allowed=False,
            reason=f"membership_check_failed: {type(exc).__name__}: {str(exc)[:120]}",
        )


def _actor_from(credentials: Any) -> str:
    """The best available name for whoever is running this. Never a caller-supplied string.

    Preference order is the same one `gcloud` itself reports: the credential's service-account
    email, then its id-token subject, then the ADC user's email. Falls back to a marker rather than
    to a blank, so an audit row always says *something* about provenance.

    The third step is a network call, and it is the one every human operator takes: a user ADC
    (`gcloud auth application-default login`) is a ``google.oauth2.credentials.Credentials`` that
    carries NO email attribute at all, so until 2026-09-10 this function fell straight through to
    ``$USER`` for every person and the membership query asked Cloud Identity about ``"thomas"``,
    which it answers with a 403 that reads as a permissions problem. The first real run of the
    tool refused its own author that way. The OpenID ``userinfo`` endpoint returns the ADC user's
    email under the scopes that login grants by default, which is what `gcloud auth list` shows.
    """
    for attribute in ("service_account_email", "signer_email", "quota_project_id"):
        value = getattr(credentials, attribute, None)
        if isinstance(value, str) and "@" in value:
            return value
    for attribute in ("_account", "account"):
        value = getattr(credentials, attribute, None)
        if isinstance(value, str) and value:
            return value
    email = _email_from_userinfo(credentials)
    if email is not None:
        return email
    return os.environ.get("USER") or "unknown"


def _email_from_userinfo(credentials: Any) -> str | None:
    """The ADC user's email from Google's OpenID ``userinfo`` endpoint, or None on any failure.

    Best-effort by design: a failure here only means the caller falls through to the ``$USER``
    marker, which :func:`_unresolved_actor_reason` then refuses with a reason naming this step.
    """
    if credentials is None or not hasattr(credentials, "token"):
        return None
    try:
        # google-auth's own session attaches and refreshes the ADC token itself; no header is
        # built here, and nothing in this file ever presents a credential of its own. The quota
        # project is dropped for this one call: a user ADC carries one (`gcloud auth
        # application-default set-quota-project`), google-auth sends it as `x-goog-user-project`,
        # and the OpenID endpoint -- not a GCP API -- answers 403 "Caller does not have required
        # permission to use project" to a caller who cannot bill that project. Measured
        # 2026-09-10; the membership call keeps the quota project, Cloud Identity accepts it.
        from google.auth.transport.requests import AuthorizedSession

        plain = credentials.with_quota_project(None)
        response = AuthorizedSession(plain).get(_USERINFO_URL, timeout=10)
        response.raise_for_status()
        email = response.json().get("email")
    except Exception:  # noqa: BLE001 - best-effort, see the docstring
        return None
    return email if isinstance(email, str) and "@" in email else None


_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def _unresolved_actor_reason(actor: str) -> str | None:
    """Why `actor` cannot be put to Cloud Identity as a member key, or None when it can.

    A member key is an email. Anything else -- the ``$USER`` marker, ``unknown`` -- is refused
    HERE rather than sent, because Cloud Identity's answer to a non-email key is a 403
    ``Insufficient permissions to retrieve memberships``, indistinguishable from an IAM gap.
    """
    if "@" in actor:
        return None
    return (
        f"actor_unresolved: ADC identifies nobody by email (got {actor!r}); run "
        "`gcloud auth application-default login` as your entur.org user"
    )


def _authorize(
    environment: str,
    action: str,
    authorizer: Authorizer,
    *,
    client_id: str | None,
    destination_id: str | None,
    key_id: str | None,
) -> str:
    """Authorize, and audit a refusal before raising. Returns the actor on success."""
    verdict = authorizer(environment)
    if verdict.allowed:
        return verdict.actor
    # Best effort, and loud regardless. See the module docstring: the same IAM that gates the action
    # may gate this write, so stderr is the record that always exists.
    print(
        json.dumps(
            {
                "event": "notification_admin",
                "result": "refused",
                "action": action,
                "actor": verdict.actor,
                "group": verdict.group,
                "environment": environment,
                "reason": verdict.reason,
                "at": datetime.now(UTC).isoformat(),
            }
        ),
        file=sys.stderr,
    )
    try:
        notification_auth.record_admin_action(
            actor=verdict.actor,
            action=action,
            result="refused",
            client_id=client_id,
            destination_id=destination_id,
            key_id=key_id,
            reason=verdict.reason or "not_authorized",
        )
    except Exception:
        logger.warning("Could not write the refusal audit record", exc_info=True)
    raise Refused(
        f"{verdict.actor} is not a member of {verdict.group} "
        f"({verdict.reason or 'not_a_member'}); {action} refused"
    )


# ---- Subcommands ------------------------------------------------------------------------------
#
# Each returns a JSON-serialisable dict. Printing is one place, so every command's output is
# machine-readable and an operator can pipe it.


def _serialise(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {k: _serialise(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, (list, tuple)):
        return [_serialise(v) for v in value]
    return value


def _quotas_from_args(args: argparse.Namespace) -> dict[str, int]:
    """The quota flags that were given, by field name. Validation is the module's, not argparse's."""
    return {
        name: value
        for name, value in (
            ("max_active_subscriptions", args.max_active_subscriptions),
            ("requests_per_minute", args.requests_per_minute),
            ("daily_cap", args.daily_cap),
            ("max_pending_events", args.max_pending_events),
            ("deliveries_per_minute", args.deliveries_per_minute),
            ("concurrent_deliveries", args.concurrent_deliveries),
            ("max_devices", args.max_devices),
        )
        if value is not None
    }


def _cmd_create_client(args: argparse.Namespace, actor: str) -> Any:
    if args.client_id == varsel_state.BROWSER_CLIENT_ID:
        # RESERVED for `/varsel`'s own registrations (U13). Not one more onboarded integration: the
        # traveller behind those documents is a member of the public, so nothing may issue a
        # credential for this id and nothing may aim a webhook destination at it. The account
        # itself is created by the cutover runbook, which sets the two ceilings and no credential.
        raise SystemExit(
            f"{varsel_state.BROWSER_CLIENT_ID!r} is reserved for /varsel's own registrations; "
            "use `set-quotas` on it rather than creating it as a client"
        )
    given = _quotas_from_args(args)
    quotas = notification_auth.Quotas(**given) if given else None
    return notification_auth.create_client(
        args.client_id, name=args.name, actor=actor, quotas=quotas
    )


def _cmd_set_quotas(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.set_client_quotas(args.client_id, actor=actor, **_quotas_from_args(args))


def _cmd_suspend_client(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.set_client_suspended(
        args.client_id, suspended=True, actor=actor, reason=args.reason
    )


def _cmd_resume_client(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.set_client_suspended(args.client_id, suspended=False, actor=actor)


def _cmd_show_client(args: argparse.Namespace, actor: str) -> Any:
    del actor
    account = notification_auth.get_account(args.client_id)
    if account is None:
        raise notification_auth.NotFound("no such client")
    return {
        "account": _serialise(account),
        "credentials": _serialise(notification_auth.list_credentials(args.client_id)),
        "destination": _serialise(notification_auth.current_destination(args.client_id)),
    }


def _cmd_issue_key(args: argparse.Namespace, actor: str) -> Any:
    credential, bearer = notification_auth.issue_credential(
        args.client_id, actor=actor, label=args.label or ""
    )
    return {
        "credential": _serialise(credential),
        # The ONLY time this value exists. There is no reversible copy in the store -- only the
        # peppered hash -- so a lost credential is re-issued, never recovered. Hand it to the
        # client over a channel that is not this terminal's scrollback.
        "bearer": bearer,
        "note": (
            "Shown once. Store it in the client's secret manager, not in a ticket. Rotation is "
            "issue-key followed by revoke-key after cutover; several credentials are live at once "
            "on purpose, so the client is never down for a rotation."
        ),
    }


def _cmd_revoke_key(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.revoke_credential(args.key_id, actor=actor, reason=args.reason)


def _cmd_list_keys(args: argparse.Namespace, actor: str) -> Any:
    del actor
    return _serialise(notification_auth.list_credentials(args.client_id))


def _cmd_create_destination(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.create_destination(
        args.destination_id,
        client_id=args.client_id,
        callback_url=args.url,
        actor=actor,
    )


def _cmd_amend_destination(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.amend_destination(
        args.destination_id, callback_url=args.url, actor=actor
    )


def _cmd_pause_destination(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.pause_destination(
        args.destination_id, reason=args.reason or "operator", actor=actor
    )


def _cmd_resume_destination(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.resume_destination(args.destination_id, actor=actor)


def _cmd_show_signing_secret(args: argparse.Namespace, actor: str) -> Any:
    """Print every live generation's signing secret for one destination.

    EVERY live generation, not just the active one, because during a rotation two are valid at once
    and "which one do I install" is the first question a client asks. Both are printed with their
    generation number beside them, and a receiver that accepts either is verifying exactly what we
    send -- each attempt carries one `v1,<sig>` entry per generation.

    Nothing is stored and nothing is looked up: the secret is DERIVED from the root over
    ``destination_id || generation``, which is why the store holds no signing material and why one
    client's leaked receiver secret forges nothing for another.

    Audited before the value is produced, so a refusal partway through still leaves the row that
    says somebody asked.
    """
    destination = notification_auth.get_destination(args.destination_id)
    if destination is None:
        raise notification_auth.NotFound("no such destination")
    notification_auth.record_admin_action(
        actor=actor,
        action="show_signing_secret",
        result="disclosed",
        client_id=destination.client_id or None,
        destination_id=destination.destination_id,
        reason=f"generations={list(destination.signing_generations)}",
    )
    return {
        "destination_id": destination.destination_id,
        "client_id": destination.client_id,
        "active_generation": destination.active_generation,
        "retiring_generation": destination.retiring_generation,
        "secrets": [
            {
                "generation": generation,
                # Named as the receiver names it: the npm verifier and the Firebase adapter both
                # take a `whsec_`-prefixed string.
                "secret": webhook_signing.derive_secret(destination.destination_id, generation),
                "role": "active" if generation == destination.active_generation else "retiring",
            }
            for generation in destination.signing_generations
        ],
        "note": (
            "LIVE CREDENTIAL. Hand it over the way a password is handed over -- not in a ticket, "
            "not in chat. During a rotation both generations verify; install the active one and "
            "keep accepting the retiring one until retire-signing is run. If this value has been "
            "anywhere it should not, rotate-signing the destination."
        ),
    }


def _cmd_rotate_signing(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.rotate_signing_generation(args.destination_id, actor=actor)


def _cmd_retire_signing(args: argparse.Namespace, actor: str) -> Any:
    return notification_auth.retire_signing_generation(args.destination_id, actor=actor)


_COMMANDS: dict[str, Callable[[argparse.Namespace, str], Any]] = {
    "create-client": _cmd_create_client,
    "suspend-client": _cmd_suspend_client,
    "resume-client": _cmd_resume_client,
    "show-client": _cmd_show_client,
    "set-quotas": _cmd_set_quotas,
    "issue-key": _cmd_issue_key,
    "revoke-key": _cmd_revoke_key,
    "list-keys": _cmd_list_keys,
    "create-destination": _cmd_create_destination,
    "amend-destination": _cmd_amend_destination,
    "pause-destination": _cmd_pause_destination,
    "resume-destination": _cmd_resume_destination,
    "rotate-signing": _cmd_rotate_signing,
    "show-signing-secret": _cmd_show_signing_secret,
    "retire-signing": _cmd_retire_signing,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_notification_client",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument(
        "--project",
        default=None,
        help="GCP project to talk to. Must be --environment's own project (ent-kixavvik-<env>); "
        "anything else is refused with exit 2. Pass it to override an exported "
        "GOOGLE_CLOUD_PROJECT that names a different environment.",
    )
    parser.add_argument(
        "--database",
        default=None,
        help=f"Named Firestore database (default: ${DATABASE_ENV} or {DEFAULT_DATABASE}).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, **kwargs)

    def quota_flags(p: argparse.ArgumentParser) -> None:
        bounds = notification_auth.QUOTA_BOUNDS
        defaults = notification_auth.DEFAULT_QUOTAS
        p.add_argument(
            "--max-active-subscriptions",
            type=int,
            default=None,
            help=f"Active subscriptions this client may hold (default "
            f"{defaults.max_active_subscriptions}, at most "
            f"{bounds['max_active_subscriptions'][1]}). The create transaction counts through an "
            "aggregation, so the bound is a population judgement rather than a read-cost one.",
        )
        p.add_argument(
            "--requests-per-minute",
            type=int,
            default=None,
            help=f"Authenticated requests per minute (default {defaults.requests_per_minute}, "
            f"at most {bounds['requests_per_minute'][1]}).",
        )
        p.add_argument(
            "--max-devices",
            type=int,
            default=None,
            help=f"Browser installs this client may hold (default {defaults.max_devices}, at most "
            f"{bounds['max_devices'][1]}). Only the `web_push` channel has devices; for a webhook "
            "client this is declared and never reached.",
        )
        p.add_argument(
            "--daily-cap",
            type=int,
            default=None,
            help=f"Alert deliveries per subscription per Oslo day (default {defaults.daily_cap}, "
            f"at most {bounds['daily_cap'][1]}). Copied onto each subscription when the client "
            "writes it, so a change reaches existing subscriptions on their next PUT.",
        )
        p.add_argument(
            "--max-pending-events",
            type=int,
            default=None,
            help=f"Undelivered events one destination may hold before emit refuses it (default "
            f"{defaults.max_pending_events}, at most {bounds['max_pending_events'][1]}).",
        )
        p.add_argument(
            "--deliveries-per-minute",
            type=int,
            default=None,
            help=f"Delivery attempts one destination may be given in a pass (default "
            f"{defaults.deliveries_per_minute}, at most {bounds['deliveries_per_minute'][1]}). "
            "Per minute rather than per pass because a pass is what the delivery worker takes "
            "away; while delivery runs on the poller thread this is an upper bound it cannot "
            "exceed.",
        )
        p.add_argument(
            "--concurrent-deliveries",
            type=int,
            default=None,
            help=f"In-flight POSTs to one destination per worker replica (default "
            f"{defaults.concurrent_deliveries}, at most {bounds['concurrent_deliveries'][1]}). "
            "Declared and not yet read: the pooled sender that spends it is U8.",
        )

    p = add(
        "create-client",
        help="Provision a client. Idempotent on (id, name); quota flags apply to a NEW account only.",
    )
    p.add_argument("--client-id", required=True)
    p.add_argument("--name", required=True)
    quota_flags(p)

    p = add("suspend-client", help="Refuse this client's credentials with 403.")
    p.add_argument("--client-id", required=True)
    p.add_argument("--reason", default=None)

    p = add("resume-client", help="Lift a suspension.")
    p.add_argument("--client-id", required=True)

    p = add("show-client", help="Account, credentials (never secrets) and current destination.")
    p.add_argument("--client-id", required=True)
    p = add(
        "set-quotas",
        help="Raise or lower a client's ceilings; unnamed ones keep their value. Audited.",
    )
    p.add_argument("--client-id", required=True)
    quota_flags(p)

    p = add("issue-key", help="Mint a credential. The secret is printed ONCE.")
    p.add_argument("--client-id", required=True)
    p.add_argument("--label", default=None, help="Free text, e.g. 'rotation 2026-09'.")

    p = add("revoke-key", help="Revoke one key-id. Terminal.")
    p.add_argument("--key-id", required=True)
    p.add_argument("--reason", default=None)

    p = add("list-keys", help="A client's key ids and their state. Never a secret.")
    p.add_argument("--client-id", required=True)

    p = add("create-destination", help="Provision a callback. The URL is address-tested first.")
    p.add_argument("--client-id", required=True)
    p.add_argument("--destination-id", required=True)
    p.add_argument("--url", required=True)

    p = add("amend-destination", help="Point a destination at a new URL.")
    p.add_argument("--destination-id", required=True)
    p.add_argument("--url", required=True)

    p = add("pause-destination", help="Stop delivery to this destination.")
    p.add_argument("--destination-id", required=True)
    p.add_argument("--reason", default=None)

    p = add("resume-destination", help="Lift a pause (including one a 410 caused).")
    p.add_argument("--destination-id", required=True)

    p = add(
        "show-signing-secret",
        help="Print the live signing secret(s) for a destination. PRINTS A LIVE CREDENTIAL.",
    )
    p.add_argument("--destination-id", required=True)

    p = add("rotate-signing", help="Mint the next signing generation; the current one retires.")
    p.add_argument("--destination-id", required=True)

    p = add("retire-signing", help="Drop the retiring generation after the client has cut over.")
    p.add_argument("--destination-id", required=True)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    authorizer: Authorizer | None = None,
    store: Any | None = None,
    out: Any | None = None,
) -> int:
    """Run one subcommand. Returns a process exit code.

    ``authorizer`` and ``store`` are the injection seams: the suite passes a fake authorizer and an
    in-memory document store, so every command is exercised with no GCP project, no group and no
    network. In production both default to the real ones, and the store is configured from
    ``--project``/``--database`` before anything is read.
    """
    args = _parser().parse_args(argv)
    stream = out or sys.stdout
    # THE GATE AND THE TARGET ARE ONE CHOICE. Resolved exactly as the app resolves it -- the flag,
    # then the ambient variable, then the environment's own project -- and refused when the answer
    # is not the project `--environment` was authorized against. That also removes the failure mode
    # `--project` was added for (a stale shell variable) rather than papering over it: an exported
    # `GOOGLE_CLOUD_PROJECT` from another environment now stops the run instead of silently
    # redirecting it.
    expected = PROJECTS[args.environment]
    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or expected
    if project != expected:
        print(
            f"--environment {args.environment} is authorized against "
            f"{OPERATOR_GROUPS[args.environment]} and acts on {expected}; refusing to run against "
            f"{project}.",
            file=sys.stderr,
        )
        return 2
    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    # `--database` deliberately keeps its free choice: a Firestore database is scoped INSIDE a
    # project, so it cannot reach another environment's data and the refusal above already bounds
    # what any value of it can touch. Naming the wrong database in the right project reads as an
    # empty store, which is loud enough.
    if store is not None:
        notification_auth.configure(docs=store)
    else:
        # `notification_store` is the one place that names the database -- `notification_auth` reads through
        # its client -- so the tool configures it exactly as `main.lifespan` does.
        notification_store.configure(
            database=args.database or os.environ.get(DATABASE_ENV) or DEFAULT_DATABASE
        )
        notification_auth.configure()
    # The one subcommand that needs the pepper, checked BEFORE the group so the message an operator
    # gets is about the variable rather than about a Helm secret group they are not looking at.
    # Every other command works without it: the pepper hashes a new secret and verifies an existing
    # one, and nothing here verifies.
    # The signing root, checked the same way and for the same reason: a failure deeper in raises
    # `SigningNotConfigured`, whose message is about provisioning Secret Manager and the chart's
    # secret group -- the wrong hint for a human at a terminal who has simply not exported it.
    if args.command == "show-signing-secret":
        try:
            webhook_signing.signing_root()
        except webhook_signing.SigningNotConfigured as exc:
            # The module's OWN message, not a paraphrase: there are two reasons the root is
            # unusable and they need different actions. "Unset or too short" is a missing export;
            # "set to the example root published in signing-vectors.json" is a paste of the
            # documented value -- and THIS command is where that paste happens, because the runbook
            # tells an operator to export the variable by hand for it. A single restated
            # "is unset (or under N bytes)" told the second operator the variable they had plainly
            # set was missing.
            print(
                f"{exc} show-signing-secret DERIVES the secret from it, so export this "
                "environment's value from Secret Manager for this one command -- see the module "
                "docstring.",
                file=sys.stderr,
            )
            return 2
    if args.command == "issue-key" and not notification_auth.pepper_ready():
        print(
            f"{notification_auth.PEPPER_ENV} is unset (or under {notification_auth.MIN_PEPPER_BYTES} bytes). "
            "issue-key hashes the new secret under it, so export this environment's value from "
            "Secret Manager for this one command -- see the module docstring.",
            file=sys.stderr,
        )
        return 2
    check = authorizer or google_group_authorizer
    try:
        actor = _authorize(
            args.environment,
            args.command,
            check,
            client_id=getattr(args, "client_id", None),
            destination_id=getattr(args, "destination_id", None),
            key_id=getattr(args, "key_id", None),
        )
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    try:
        result = _COMMANDS[args.command](args, actor)
    except notification_auth.ClientAuthError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(_serialise(result), indent=2, ensure_ascii=False), file=stream)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
