FROM python:3.13-alpine

ARG MCP_EMAIL_SERVER_VERSION=0.16.0

RUN apk add --no-cache ca-certificates \
    && pip install --no-cache-dir "mcp-email-server==${MCP_EMAIL_SERVER_VERSION}"

RUN addgroup -g 10000 mailmcp \
    && adduser -D -u 10000 -G mailmcp mailmcp

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

USER mailmcp

EXPOSE 9557

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

CMD ["mcp-email-server", "streamable-http"]
