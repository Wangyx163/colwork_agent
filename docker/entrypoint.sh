#!/bin/sh
# Materialise .env.local from the container environment, then hand over.
#
# The project reads DATABASE_URL only from .env.local and deliberately not from
# the process environment: the workbench and the Agent Worker are separate
# processes that must agree on one database, and an ambient DATABASE_URL from
# somewhere else is exactly the accident that would split them. A container has
# no such file, so it is written here from values compose passes in.
set -eu

ENV_FILE=/app/.env.local

if [ -n "${DATABASE_URL:-}" ]; then
    : > "$ENV_FILE"
    printf 'DATABASE_URL=%s\n' "$DATABASE_URL" >> "$ENV_FILE"

    if [ -n "${COLWORK_SESSION_SECRET:-}" ]; then
        printf 'COLWORK_SESSION_SECRET=%s\n' "$COLWORK_SESSION_SECRET" >> "$ENV_FILE"
    fi
    if [ -n "${COLWORK_RESULT_PROCESSING_MODE:-}" ]; then
        printf 'COLWORK_RESULT_PROCESSING_MODE=%s\n' \
            "$COLWORK_RESULT_PROCESSING_MODE" >> "$ENV_FILE"
    fi
    # Feishu credentials are read from the process environment first, so they
    # do not strictly need to be here -- written anyway so one file shows the
    # whole configuration when debugging inside the container.
    if [ -n "${FEISHU_APP_ID:-}" ]; then
        printf 'FEISHU_APP_ID=%s\n' "$FEISHU_APP_ID" >> "$ENV_FILE"
    fi
    if [ -n "${FEISHU_APP_SECRET:-}" ]; then
        printf 'FEISHU_APP_SECRET=%s\n' "$FEISHU_APP_SECRET" >> "$ENV_FILE"
    fi

    chmod 600 "$ENV_FILE"
fi

exec "$@"
