from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Annotated, Literal
import unicodedata

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
    sender_equals: tuple[str, ...] = ()
    sender_contains: tuple[str, ...] = ()
    body_contains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not any(
            (
                self.subject_equals,
                self.subject_contains,
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
)

PAGE_SIZE = 100
BODY_BATCH_SIZE = 50

TRASH_FALLBACK_NAMES = (
    "Trash",
    "Deleted Items",
    "Deleted Messages",
    "Papierkorb",
    "Gelöschte Elemente",
    "Gelöscht",
)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    value_normalized = _normalize_text(value)
    return any(_normalize_text(needle) in value_normalized for needle in needles)


def _equals_any(value: str, expected: tuple[str, ...]) -> bool:
    value_normalized = _normalize_text(value)
    return any(_normalize_text(item) == value_normalized for item in expected)


def _sender_address(sender: str) -> str:
    return parseaddr(sender)[1] or sender


def _metadata_matches(email: EmailMetadata, rule: MailRule) -> bool:
    if rule.subject_equals and not _equals_any(email.subject, rule.subject_equals):
        return False

    if rule.subject_contains and not _contains_any(email.subject, rule.subject_contains):
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


if __name__ == "__main__":
    streamable_http()
