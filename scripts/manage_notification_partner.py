#!/usr/bin/env python3
"""Operator tool for the partner notification channel: partners, credentials, destinations.

R22's boundary, as a command-line tool rather than an API surface. Nothing here is reachable from
the running app: provisioning a partner, issuing or revoking a credential, moving or pausing a
callback destination and suspending a partner are operator actions, and putting any of them behind
a partner credential would let a partner grant itself what an operator is supposed to decide.

**Authorization.** Every subcommand -- reads included -- is authorized against a NAMED
least-privilege Google group, and production has its own:

============  =====================================================================
``dev``       ``kix-avvik-notification-operators@entur.org``
``tst``       ``kix-avvik-notification-operators@entur.org``
``prd``       ``kix-avvik-notification-operators-prd@entur.org``
============  =====================================================================

The group is what holds ``roles/datastore.user`` on that environment's GCP project, which is what
makes the IAM environment-scoped rather than global: each environment is its own project with its
own runtime identity (KTD5). Reads are gated too, deliberately: a destination's callback URL and a
partner's key ids are exactly what an attacker would want before trying to forge one.

**``--environment`` picks the target as well as the gate**, and until 2026-09-09 it picked only the
gate: ``--project`` chose which project was read and written with no comparison between the two, so
``--environment dev --project ent-kixavvik-prd`` was authorized against the dev group and then acted
on prd -- ``show-signing-secret`` printing a live prd receiver credential, ``suspend-partner``
silencing a real partner. What refused it was prd project IAM, not this tool, so R22's separate prd
group held only while that group was the sole holder of ``roles/datastore.user`` there. The tool now
resolves the project from the environment (:data:`PROJECTS`) and exits 2 when a flag or an exported
``GOOGLE_CLOUD_PROJECT`` disagrees, and IAM backs that rather than being it.

Separate for production, and separately from partner keys: a partner credential authenticates a
machine against ``/notification-api/v1`` and can do none of this, while a group membership
authorizes a human against the store and cannot register a subscription.

**Audit.** Every action lands in ``partner_admin_audit`` -- actor, action, partner, ``key-id``,
timestamp -- through :func:`partner_auth.record_admin_action`, which every mutating function in
that module calls inside its own path. A REFUSAL is audited too (scenario 10), and that has one
honest limitation: if the group IS the IAM grant, somebody who is not a member may also be unable
to write the audit collection, so the refusal record is written best-effort and a structured line
always goes to stderr. The record therefore proves what was attempted and by whom it claimed to
be, not that the claim was verified -- and it cannot be erased, because nothing in this repo
updates or deletes a row in that collection.

**Running it.** The group check needs the Cloud Identity client, which is **not** a dependency of
this repo -- nothing in the image talks to it, and adding it to ``pyproject.toml`` would put it in
every developer's environment for a tool nobody runs from CI. So it is named on the command line:

    uv run --with google-api-python-client python scripts/manage_notification_partner.py \\
        --environment dev create-partner --partner-id acme --name "Acme Reise AS"

Without that flag :func:`google_group_authorizer` cannot import the client and **fails closed** --
it reports ``membership_check_failed`` and every subcommand is refused. That is the right failure
direction and a confusing one to hit by accident, so the missing flag is the first thing to check
when a command is refused for somebody who *is* in the group.

**``issue-key`` additionally needs that environment's ``PARTNER_KEY_PEPPER`` exported**, because a
credential's secret is stored only as its HMAC under the pepper and there is nothing to hash
without it. No other subcommand touches it. Fetch it from Secret Manager into the shell for the one
command and drop it afterwards -- never paste it from a ticket, and never leave it exported:

    PARTNER_KEY_PEPPER="$(gcloud secrets versions access latest \\
        --secret=PARTNER_KEY_PEPPER --project=ent-kixavvik-dev)" \\
      uv run --with google-api-python-client python scripts/manage_notification_partner.py \\
        --environment dev issue-key --partner-id acme

That is a real exposure and is named rather than buried: for the duration of one key issue the
pepper is on an operator's machine. It is also why the pre-check below refuses early with a
one-line message about the variable -- the alternative is a failure whose text talks about a Helm
secret group, which is the wrong hint for a human at a terminal.

**``show-signing-secret`` needs that environment's ``PARTNER_SIGNING_ROOT`` exported**, for the
same reason and with the same early pre-check. **It prints a live credential**: the value a
partner's receiver verifies every webhook signature with. It is the ONE command whose output must
not go into a ticket, a chat message or a shell history file -- hand it over the way a password is
handed over, and rotate the destination's generation (``rotate-signing``) if it has been anywhere
else. It is audited like every other action here, because a credential disclosure is precisely what
an audit trail exists for: the row says who read it, for which destination, and when.

It exists because there is otherwise NO path to that value. `webhook_signing.derive_secret` is
called only by the signer; ``GET /destinations/current`` serves generation NUMBERS and never
derives, deliberately -- a secret fetchable over the partner's own credential is a secret their
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
# it to sys.path is what makes `import partner_auth` resolve the same way it does in the app.
_API = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))

import partner_auth
import partner_store
import webhook_signing

logger = logging.getLogger("manage_notification_partner")

ENVIRONMENTS = ("dev", "tst", "prd")

#: R22's named groups. Production is its own, so membership for the environments where a mistake is
#: cheap does not carry into the one where it is not.
OPERATOR_GROUPS = {
    "dev": "kix-avvik-notification-operators@entur.org",
    "tst": "kix-avvik-notification-operators@entur.org",
    "prd": "kix-avvik-notification-operators-prd@entur.org",
}

#: WHICH PROJECT EACH ENVIRONMENT IS, so the gate and the target cannot be chosen separately.
#: `--environment` picks the group membership is checked against and `--project` used to pick, with
#: no comparison between them, which project was then read and written -- so `--environment dev
#: --project ent-kixavvik-prd` was authorized against the dev group and acted on prd, including
#: `show-signing-secret` (prints a live prd receiver credential) and `suspend-partner` (silences a
#: real partner). What stopped it was prd project IAM rather than this tool, while the module
#: docstring claimed the stronger property; now the tool refuses first and IAM backs it.
#:
#: The naming assumption is `ent-kixavvik-<env>`, which is what `.entur/application.yaml`'s own
#: verification recipe uses. If a project is ever renamed this map is the one place to change.
PROJECTS = {environment: f"ent-kixavvik-{environment}" for environment in ENVIRONMENTS}

#: The named database `partner_store` and `partner_auth` open. The same literal lives in the Helm
#: chart's `PARTNER_FIRESTORE_DATABASE` and in `terraform/variables.tf`; it is read from the
#: environment here so a tool run against a differently-named database does not need a code change,
#: and it defaults to the one every environment uses.
DATABASE_ENV = "PARTNER_FIRESTORE_DATABASE"
DEFAULT_DATABASE = "partner-notifications"


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
            .checkTransitiveMembership(parent=lookup["name"], query=f'member_key_id == "{actor}"')
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
    """
    for attribute in ("service_account_email", "signer_email", "quota_project_id"):
        value = getattr(credentials, attribute, None)
        if isinstance(value, str) and "@" in value:
            return value
    for attribute in ("_account", "account"):
        value = getattr(credentials, attribute, None)
        if isinstance(value, str) and value:
            return value
    return os.environ.get("USER") or "unknown"


def _authorize(
    environment: str,
    action: str,
    authorizer: Authorizer,
    *,
    partner_id: str | None,
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
                "event": "partner_admin",
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
        partner_auth.record_admin_action(
            actor=verdict.actor,
            action=action,
            result="refused",
            partner_id=partner_id,
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


def _cmd_create_partner(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.create_partner(args.partner_id, name=args.name, actor=actor)


def _cmd_suspend_partner(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.set_partner_suspended(
        args.partner_id, suspended=True, actor=actor, reason=args.reason
    )


def _cmd_resume_partner(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.set_partner_suspended(args.partner_id, suspended=False, actor=actor)


def _cmd_show_partner(args: argparse.Namespace, actor: str) -> Any:
    del actor
    account = partner_auth.get_account(args.partner_id)
    if account is None:
        raise partner_auth.NotFound("no such partner")
    return {
        "account": _serialise(account),
        "credentials": _serialise(partner_auth.list_credentials(args.partner_id)),
        "destination": _serialise(partner_auth.current_destination(args.partner_id)),
    }


def _cmd_issue_key(args: argparse.Namespace, actor: str) -> Any:
    credential, bearer = partner_auth.issue_credential(
        args.partner_id, actor=actor, label=args.label or ""
    )
    return {
        "credential": _serialise(credential),
        # The ONLY time this value exists. There is no reversible copy in the store -- only the
        # peppered hash -- so a lost credential is re-issued, never recovered. Hand it to the
        # partner over a channel that is not this terminal's scrollback.
        "bearer": bearer,
        "note": (
            "Shown once. Store it in the partner's secret manager, not in a ticket. Rotation is "
            "issue-key followed by revoke-key after cutover; several credentials are live at once "
            "on purpose, so the partner is never down for a rotation."
        ),
    }


def _cmd_revoke_key(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.revoke_credential(args.key_id, actor=actor, reason=args.reason)


def _cmd_list_keys(args: argparse.Namespace, actor: str) -> Any:
    del actor
    return _serialise(partner_auth.list_credentials(args.partner_id))


def _cmd_create_destination(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.create_destination(
        args.destination_id,
        partner_id=args.partner_id,
        callback_url=args.url,
        actor=actor,
    )


def _cmd_amend_destination(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.amend_destination(
        args.destination_id, callback_url=args.url, actor=actor
    )


def _cmd_pause_destination(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.pause_destination(
        args.destination_id, reason=args.reason or "operator", actor=actor
    )


def _cmd_resume_destination(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.resume_destination(args.destination_id, actor=actor)


def _cmd_show_signing_secret(args: argparse.Namespace, actor: str) -> Any:
    """Print every live generation's signing secret for one destination.

    EVERY live generation, not just the active one, because during a rotation two are valid at once
    and "which one do I install" is the first question a partner asks. Both are printed with their
    generation number beside them, and a receiver that accepts either is verifying exactly what we
    send -- each attempt carries one `v1,<sig>` entry per generation.

    Nothing is stored and nothing is looked up: the secret is DERIVED from the root over
    ``destination_id || generation``, which is why the store holds no signing material and why one
    partner's leaked receiver secret forges nothing for another.

    Audited before the value is produced, so a refusal partway through still leaves the row that
    says somebody asked.
    """
    destination = partner_auth.get_destination(args.destination_id)
    if destination is None:
        raise partner_auth.NotFound("no such destination")
    partner_auth.record_admin_action(
        actor=actor,
        action="show_signing_secret",
        result="disclosed",
        partner_id=destination.partner_id or None,
        destination_id=destination.destination_id,
        reason=f"generations={list(destination.signing_generations)}",
    )
    return {
        "destination_id": destination.destination_id,
        "partner_id": destination.partner_id,
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
    return partner_auth.rotate_signing_generation(args.destination_id, actor=actor)


def _cmd_retire_signing(args: argparse.Namespace, actor: str) -> Any:
    return partner_auth.retire_signing_generation(args.destination_id, actor=actor)


_COMMANDS: dict[str, Callable[[argparse.Namespace, str], Any]] = {
    "create-partner": _cmd_create_partner,
    "suspend-partner": _cmd_suspend_partner,
    "resume-partner": _cmd_resume_partner,
    "show-partner": _cmd_show_partner,
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
        prog="manage_notification_partner",
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

    p = add("create-partner", help="Provision a partner. Idempotent on (id, name).")
    p.add_argument("--partner-id", required=True)
    p.add_argument("--name", required=True)

    p = add("suspend-partner", help="Refuse this partner's credentials with 403.")
    p.add_argument("--partner-id", required=True)
    p.add_argument("--reason", default=None)

    p = add("resume-partner", help="Lift a suspension.")
    p.add_argument("--partner-id", required=True)

    p = add("show-partner", help="Account, credentials (never secrets) and current destination.")
    p.add_argument("--partner-id", required=True)

    p = add("issue-key", help="Mint a credential. The secret is printed ONCE.")
    p.add_argument("--partner-id", required=True)
    p.add_argument("--label", default=None, help="Free text, e.g. 'rotation 2026-09'.")

    p = add("revoke-key", help="Revoke one key-id. Terminal.")
    p.add_argument("--key-id", required=True)
    p.add_argument("--reason", default=None)

    p = add("list-keys", help="A partner's key ids and their state. Never a secret.")
    p.add_argument("--partner-id", required=True)

    p = add("create-destination", help="Provision a callback. The URL is address-tested first.")
    p.add_argument("--partner-id", required=True)
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

    p = add("retire-signing", help="Drop the retiring generation after the partner has cut over.")
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
        partner_auth.configure(docs=store)
    else:
        # `partner_store` is the one place that names the database -- `partner_auth` reads through
        # its client -- so the tool configures it exactly as `main.lifespan` does.
        partner_store.configure(
            database=args.database or os.environ.get(DATABASE_ENV) or DEFAULT_DATABASE
        )
        partner_auth.configure()
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
    if args.command == "issue-key" and not partner_auth.pepper_ready():
        print(
            f"{partner_auth.PEPPER_ENV} is unset (or under {partner_auth.MIN_PEPPER_BYTES} bytes). "
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
            partner_id=getattr(args, "partner_id", None),
            destination_id=getattr(args, "destination_id", None),
            key_id=getattr(args, "key_id", None),
        )
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    try:
        result = _COMMANDS[args.command](args, actor)
    except partner_auth.PartnerAuthError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(_serialise(result), indent=2, ensure_ascii=False), file=stream)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
