#!/bin/sh
set -eu

PASSWORD_FILE="${MCP_EMAIL_SERVER_PASSWORD_FILE:-/run/secrets/mailbox_password}"

if [ -n "${MCP_EMAIL_SERVER_PASSWORD:-}" ]; then
    echo "ERROR: MCP_EMAIL_SERVER_PASSWORD must not be provided directly" >&2
    exit 1
fi

if [ ! -r "$PASSWORD_FILE" ]; then
    echo "ERROR: mailbox password secret is not readable: $PASSWORD_FILE" >&2
    exit 1
fi

PASSWORD="$(cat "$PASSWORD_FILE")"

if [ -z "$PASSWORD" ]; then
    echo "ERROR: mailbox password secret is empty" >&2
    exit 1
fi

export MCP_EMAIL_SERVER_PASSWORD="$PASSWORD"
unset PASSWORD

exec "$@"
