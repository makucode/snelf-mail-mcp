# snelf-mail-mcp

Mailbox.org MCP sidecar for Snelf.

Uses:

- mcp-email-server 0.16.0
- native MCP Streamable HTTP
- Docker secrets for mailbox credentials

## Architecture

```text
Snelf / Hermes
      |
      | Streamable HTTP
      v
snelf-mail-mcp
      |
      | IMAP/TLS
      v
mailbox.org
