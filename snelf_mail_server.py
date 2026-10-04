from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
import re
from typing import Annotated, Literal
import unicodedata
from urllib.parse import urlparse

from pydantic import Field

from mcp_email_server.app import mcp
from mcp_email_server.cli import streamable_http
from mcp_email_server.emails.dispatcher import dispatch_handler
from mcp_email_server.emails.models import EmailMetadata


Action = Literal["trash"]


@dataclass(frozen=True)
class MailRule:
    name: str
    action: Action = "trash"

    # OR within one field, AND across configured fields.
    subject_equals: tuple[str, ...] = ()
    subject_contains: tuple[str, ...] = ()
    # OR across groups, AND within each group. Term order is irrelevant.
    subject_contains_groups: tuple[tuple[str, ...], ...] = ()
    sender_equals: tuple[str, ...] = ()
    sender_contains: tuple[str, ...] = ()
    body_contains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not any(
            (
                self.subject_equals,
                self.subject_contains,
                self.subject_contains_groups,
                self.sender_equals,
                self.sender_contains,
                self.body_contains,
            )
        ):
            raise ValueError(f"Rule {self.name!r} has no match criteria")


RULES: tuple[MailRule, ...] = (
    MailRule(
        name="dott_1_eur",
        subject_equals=("Dott (emTransit BV): 1,00 € EUR",),
    ),
    MailRule(
        name="dott_1_35_eur",
        subject_equals=("Dott (emTransit BV): 1,35 € EUR",),
    ),
    MailRule(
        name="email_address_confirmation",
        subject_contains_groups=(
            ("bestätig", "e-mail-adresse"),
            ("anmeldung", "e-mail-adresse"),
            ("änder", "e-mail-adresse"),
            ("verifizier", "e-mail-adresse"),
            ("bestätigungscode",),
            ("änder", "konto"),
        ),
    ),
    MailRule(
        name="klarna_paypal_codes",
        subject_contains=("code",),
        sender_contains=("klarna", "paypal"),
    ),
    MailRule(
        name="amazon_invitation_request",
        subject_equals=("Einladungsanfrage erhalten",),
        sender_contains=("amazon",),
    ),
)

PAGE_SIZE = 100
BODY_BATCH_SIZE = 50
SORT_SNIPPET_LENGTH = 500
SORT_BODY_SCAN_LENGTH = 4000
MAX_LINK_DOMAINS = 8

SORT_DESTINATIONS = (
    "Newsletter",
    "Urlaub & Hotels",
    "Bestellungen & Einkäufe",
    "Finanzen",
    "Social & Plattformen",
)

_URL_RE = re.compile(r"""https?://[^\s<>'"\]\)]+""", re.IGNORECASE)

TRASH_FALLBACK_NAMES = (
    "Trash",
    "Deleted Items",
    "Deleted Messages",
    "Papierkorb",
    "Gelöschte Elemente",
    "Gelöscht",
)


def _compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split())


def _normalize_text(value: str) -> str:
    return _compact_text(value).casefold()


def _body_snippet(body: str) -> str:
    compact = _compact_text(body)
    if len(compact) <= SORT_SNIPPET_LENGTH:
        return compact
    return compact[:SORT_SNIPPET_LENGTH].rstrip() + "…"


def _link_domains(body: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()

    for match in _URL_RE.findall(body):
        try:
            hostname = (urlparse(match).hostname or "").casefold().rstrip(".")
        except ValueError:
            # Malformed URLs in untrusted email bodies must not abort an
            # otherwise valid INBOX sort batch (e.g. "Invalid IPv6 URL").
            continue

        if hostname.startswith("www."):
            hostname = hostname[4:]
        if not hostname or hostname in seen:
            continue

        seen.add(hostname)
        domains.append(hostname)
        if len(domains) >= MAX_LINK_DOMAINS:
            break

    return domains


def _sender_domain(sender: str) -> str:
    address = _sender_address(sender)
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].casefold()


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    value_normalized = _normalize_text(value)
    return any(_normalize_text(needle) in value_normalized for needle in needles)


def _equals_any(value: str, expected: tuple[str, ...]) -> bool:
    value_normalized = _normalize_text(value)
    return any(_normalize_text(item) == value_normalized for item in expected)


def _contains_any_group(value: str, groups: tuple[tuple[str, ...], ...]) -> bool:
    value_normalized = _normalize_text(value)
    return any(
        all(_normalize_text(term) in value_normalized for term in group)
        for group in groups
    )


def _sender_address(sender: str) -> str:
    return parseaddr(sender)[1] or sender


def _metadata_matches(email: EmailMetadata, rule: MailRule) -> bool:
    if rule.subject_equals and not _equals_any(email.subject, rule.subject_equals):
        return False

    if rule.subject_contains and not _contains_any(email.subject, rule.subject_contains):
        return False

    if rule.subject_contains_groups and not _contains_any_group(
        email.subject, rule.subject_contains_groups
    ):
        return False

    sender_address = _sender_address(email.sender)

    if rule.sender_equals and not _equals_any(sender_address, rule.sender_equals):
        return False

    if rule.sender_contains and not (
        _contains_any(sender_address, rule.sender_contains)
        or _contains_any(email.sender, rule.sender_contains)
    ):
        return False

    return True


async def _find_trash_mailbox(handler) -> str | None:
    mailboxes = await handler.list_mailboxes()

    for mailbox in mailboxes:
        if any(flag.casefold() == r"\trash".casefold() for flag in mailbox.flags):
            return mailbox.name

    by_name = {mailbox.name.casefold(): mailbox.name for mailbox in mailboxes}
    for fallback in TRASH_FALLBACK_NAMES:
        match = by_name.get(fallback.casefold())
        if match:
            return match

    return None


async def _list_candidates(handler, rule: MailRule) -> list[EmailMetadata]:
    # Use one server-side filter when it is unambiguous, then verify every
    # configured condition locally. This keeps the rule model extensible while
    # avoiding a full INBOX scan for the common exact-subject/sender cases.
    subject_filter = rule.subject_equals[0] if len(rule.subject_equals) == 1 else None
    sender_filter = rule.sender_equals[0] if len(rule.sender_equals) == 1 else None

    page = 1
    emails: list[EmailMetadata] = []

    while True:
        result = await handler.get_emails_metadata(
            page=page,
            page_size=PAGE_SIZE,
            subject=subject_filter,
            from_address=sender_filter,
            mailbox="INBOX",
        )
        emails.extend(result.emails)

        if page * result.page_size >= result.total:
            break

        page += 1

    return emails


async def _filter_by_body(handler, emails: list[EmailMetadata], rule: MailRule) -> list[EmailMetadata]:
    if not rule.body_contains or not emails:
        return emails

    by_id = {email.email_id: email for email in emails}
    matched_ids: set[str] = set()

    ids = list(by_id)
    for start in range(0, len(ids), BODY_BATCH_SIZE):
        batch_ids = ids[start : start + BODY_BATCH_SIZE]
        result = await handler.get_emails_content(
            batch_ids,
            mailbox="INBOX",
            mark_as_read=False,
            max_body_length=20000,
        )

        for email in result.emails:
            if _contains_any(email.body, rule.body_contains):
                matched_ids.add(email.email_id)

    return [email for email in emails if email.email_id in matched_ids]


async def _run_prefilter(handler) -> dict:
    trash_mailbox = await _find_trash_mailbox(handler)

    if trash_mailbox is None:
        return {
            "status": "skipped",
            "reason": "trash_mailbox_not_found",
            "matched": 0,
            "moved": 0,
            "failed": 0,
            "rules": {},
        }

    matched_by_rule: dict[str, list[str]] = {}
    ids_to_move: list[str] = []
    seen_ids: set[str] = set()

    for rule in RULES:
        candidates = await _list_candidates(handler, rule)
        matches = [email for email in candidates if _metadata_matches(email, rule)]
        matches = await _filter_by_body(handler, matches, rule)

        matched_ids = [email.email_id for email in matches]
        matched_by_rule[rule.name] = matched_ids

        for email_id in matched_ids:
            if email_id not in seen_ids:
                seen_ids.add(email_id)
                ids_to_move.append(email_id)

    if not ids_to_move:
        return {
            "status": "ok",
            "trash_mailbox": trash_mailbox,
            "matched": 0,
            "moved": 0,
            "failed": 0,
            "rules": {name: 0 for name in matched_by_rule},
        }

    moved_ids, failed_ids = await handler.move_emails(
        ids_to_move,
        source_mailbox="INBOX",
        destination_mailbox=trash_mailbox,
    )
    moved_set = set(moved_ids)

    return {
        "status": "ok" if not failed_ids else "partial",
        "trash_mailbox": trash_mailbox,
        "matched": len(ids_to_move),
        "moved": len(moved_ids),
        "failed": len(failed_ids),
        "rules": {
            rule_name: sum(email_id in moved_set for email_id in email_ids)
            for rule_name, email_ids in matched_by_rule.items()
        },
    }


async def _list_inbox_metadata(handler) -> list[EmailMetadata]:
    page = 1
    emails: list[EmailMetadata] = []

    while True:
        result = await handler.get_emails_metadata(
            page=page,
            page_size=PAGE_SIZE,
            mailbox="INBOX",
        )
        emails.extend(result.emails)

        if page * result.page_size >= result.total:
            break
        page += 1

    return emails


async def _fetch_sort_bodies(handler, email_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    bodies: dict[str, str] = {}
    failed_ids: list[str] = []

    for start in range(0, len(email_ids), BODY_BATCH_SIZE):
        batch_ids = email_ids[start : start + BODY_BATCH_SIZE]
        result = await handler.get_emails_content(
            batch_ids,
            mailbox="INBOX",
            mark_as_read=False,
            max_body_length=SORT_BODY_SCAN_LENGTH,
        )

        for email in result.emails:
            bodies[email.email_id] = email.body

        failed_ids.extend(result.failed_ids)

    return bodies, failed_ids


@mcp.tool(
    description=(
        "Run Snelf's deterministic INBOX prefilter before semantic mail sorting. "
        "Matches only hard-coded rules and moves matches to the account's Trash mailbox. "
        "It never permanently deletes messages."
    )
)
async def prefilter_inbox(
    account_name: Annotated[
        str,
        Field(description="The configured email account to prefilter."),
    ] = "mailbox",
) -> dict:
    handler = dispatch_handler(account_name)
    return await _run_prefilter(handler)


@mcp.tool(
    description=(
        "Prepare the INBOX for one batched semantic sort. Runs the deterministic prefilter first, "
        "then returns every remaining INBOX message with compact metadata, a short body snippet, "
        "link domains and attachment hints. Use this instead of separate prefilter/list/body calls "
        "for normal mail sorting."
    )
)
async def prepare_inbox_sort(
    account_name: Annotated[
        str,
        Field(description="The configured email account to prepare."),
    ] = "mailbox",
) -> dict:
    handler = dispatch_handler(account_name)
    prefilter = await _run_prefilter(handler)

    emails = await _list_inbox_metadata(handler)
    email_ids = [email.email_id for email in emails]
    bodies, body_failed_ids = await _fetch_sort_bodies(handler, email_ids)

    prepared: list[dict] = []
    for email in emails:
        body = bodies.get(email.email_id, "")
        prepared.append(
            {
                "email_id": email.email_id,
                "from": email.sender,
                "sender_domain": _sender_domain(email.sender),
                "subject": email.subject,
                "date": email.date.isoformat(),
                "snippet": _body_snippet(body) if body else "",
                "link_domains": _link_domains(body) if body else [],
                "has_attachments": bool(email.attachments),
                "attachments": email.attachments[:5],
                "body_available": email.email_id in bodies,
            }
        )

    return {
        "status": "ok",
        "prefilter": prefilter,
        "count": len(prepared),
        "body_failed_ids": body_failed_ids,
        "emails": prepared,
    }


@mcp.tool(
    description=(
        "Apply a complete semantic INBOX sort plan in one tool call. Keys must be one of Snelf's "
        "configured destination mailboxes and values are INBOX email IDs. Omit messages that should "
        "remain in INBOX. The tool validates the whole plan before moving anything."
    )
)
async def apply_inbox_sort(
    sort_plan: Annotated[
        dict[str, list[str]],
        Field(
            description=(
                "Destination mailbox to INBOX email IDs. Allowed destinations: Newsletter, "
                "Urlaub & Hotels, Bestellungen & Einkäufe, Finanzen, Social & Plattformen."
            )
        ),
    ],
    account_name: Annotated[
        str,
        Field(description="The configured email account whose INBOX should be sorted."),
    ] = "mailbox",
) -> dict:
    handler = dispatch_handler(account_name)

    unknown_destinations = [name for name in sort_plan if name not in SORT_DESTINATIONS]
    if unknown_destinations:
        return {
            "status": "invalid_plan",
            "reason": "unknown_destination",
            "unknown_destinations": unknown_destinations,
            "allowed_destinations": list(SORT_DESTINATIONS),
        }

    assigned_to: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    normalized_plan: dict[str, list[str]] = {}

    for destination in SORT_DESTINATIONS:
        unique_ids = list(dict.fromkeys(sort_plan.get(destination, [])))
        normalized_plan[destination] = unique_ids

        for email_id in unique_ids:
            previous = assigned_to.get(email_id)
            if previous is not None and previous != destination:
                duplicates.setdefault(email_id, [previous]).append(destination)
            else:
                assigned_to[email_id] = destination

    if duplicates:
        return {
            "status": "invalid_plan",
            "reason": "email_assigned_to_multiple_destinations",
            "duplicates": duplicates,
        }

    mailboxes = await handler.list_mailboxes()
    mailbox_names = {mailbox.name.casefold(): mailbox.name for mailbox in mailboxes}

    requested_destinations = [
        destination for destination, email_ids in normalized_plan.items() if email_ids
    ]
    missing_destinations = [
        destination
        for destination in requested_destinations
        if destination.casefold() not in mailbox_names
    ]
    if missing_destinations:
        return {
            "status": "invalid_plan",
            "reason": "destination_mailbox_not_found",
            "missing_destinations": missing_destinations,
        }

    per_destination: dict[str, dict] = {}
    total_requested = 0
    total_moved = 0
    total_failed = 0

    for destination in SORT_DESTINATIONS:
        email_ids = normalized_plan[destination]
        if not email_ids:
            continue

        total_requested += len(email_ids)
        actual_destination = mailbox_names[destination.casefold()]
        moved_ids, failed_ids = await handler.move_emails(
            email_ids,
            source_mailbox="INBOX",
            destination_mailbox=actual_destination,
        )

        total_moved += len(moved_ids)
        total_failed += len(failed_ids)
        per_destination[destination] = {
            "requested": len(email_ids),
            "moved": len(moved_ids),
            "failed": len(failed_ids),
            "failed_ids": failed_ids,
        }

    return {
        "status": "ok" if total_failed == 0 else "partial",
        "requested": total_requested,
        "moved": total_moved,
        "failed": total_failed,
        "destinations": per_destination,
    }


if __name__ == "__main__":
    streamable_http()
