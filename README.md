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
