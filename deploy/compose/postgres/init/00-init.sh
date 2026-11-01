#!/bin/sh
# Runs once, on the first start of the bundled-postgres container (CI / machines
# without a local PostgreSQL). It applies the same SQL used for the host install, so
# both environments get identical roles, databases and privileges.
set -eu
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     -v owner_pw="$RAG_DB_OWNER_PASSWORD" \
     -v app_pw="$RAG_DB_APP_PASSWORD" \
     -f /opt/rag/init_local.sql
