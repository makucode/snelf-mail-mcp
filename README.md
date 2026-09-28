# snelf-mail-mcp

Mailbox.org MCP sidecar for Snelf.

Uses:

- mcp-email-server 0.16.0
- native MCP Streamable HTTP
- Docker secrets for mailbox credentials
- a small Snelf-specific extension layer for deterministic mail rules

## Architecture

```text
Snelf / Hermes
      |
      | Streamable HTTP
      v
snelf-mail-mcp
      |
      +-- upstream mcp-email-server tools
      |
      +-- prefilter_inbox
      +-- prepare_inbox_sort
      +-- apply_inbox_sort
      |
      | IMAP/TLS
      v
mailbox.org
```

## Deterministic INBOX prefilter

`prefilter_inbox` runs hard-coded, deterministic rules before semantic mail
sorting. Matching messages are moved from `INBOX` into the account's Trash
mailbox.

The prefilter never permanently deletes messages. It resolves Trash using the
IMAP `\\Trash` special-use flag first and only falls back to common Trash
folder names. If no Trash mailbox can be identified safely, the prefilter does
nothing and returns `status: skipped`.

Rules live in `snelf_mail_server.py` as `MailRule` entries:

```python
RULES = (
    MailRule(
        name="dott_1_eur",
        subject_equals=("Dott (emTransit BV): 1,00 € EUR",),
    ),
)
```

Supported match criteria:

- `subject_equals`
- `subject_contains`
- `sender_equals`
- `sender_contains`
- `body_contains`

Within one criterion multiple values are ORed. Different configured criteria
on the same rule are ANDed.

Examples:

```python
MailRule(
    name="example_sender",
    sender_equals=("billing@example.com",),
)

MailRule(
    name="example_subject_or",
    subject_equals=("Receipt A", "Receipt B"),
)

MailRule(
    name="example_combined",
    sender_contains=("@example.com",),
    subject_contains=("receipt",),
    body_contains=("automatic payment",),
)
```

All rules currently use the only supported action, `trash`.


## Batched semantic INBOX sorting

The Snelf-specific sort workflow is optimized around two MCP calls:

1. `prepare_inbox_sort`
   - runs the deterministic prefilter first;
   - reads all remaining `INBOX` metadata;
   - fetches up to 4000 body characters internally per message;
   - returns only a compact 500-character snippet plus sender domain, link
     domains, attachment hints, subject, date and email ID.
2. `apply_inbox_sort`
   - accepts the complete semantic sort plan in one call;
   - validates all destination folders and rejects an email ID assigned to
     multiple folders before moving anything;
   - batches the actual IMAP moves by destination and returns compact counts.

Messages omitted from the plan remain in `INBOX`. The allowed semantic
destinations are:

- `Newsletter`
- `Urlaub & Hotels`
- `Bestellungen & Einkäufe`
- `Finanzen`
- `Social & Plattformen`

The short snippet is intentionally a compromise between classification quality
(including spam/phishing cues) and model context size. Link domains are
extracted from the larger internal body window so suspicious destinations can
still be surfaced without sending the whole body to the model.

For ambiguous messages, the ordinary upstream `get_emails_content` tool can
still be used selectively to inspect the full content.
