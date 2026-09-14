#!/usr/bin/env sh
set -eu
: "${DATABASE_URL:?DATABASE_URL is required}"
: "${BACKUP_FILE:?BACKUP_FILE is required}"
if [ "${CONFIRM_RESTORE:-}" != "yes" ]; then
  echo "Set CONFIRM_RESTORE=yes only for the intended recovery database." >&2
  exit 2
fi
pg_restore --clean --if-exists --no-owner --dbname="$DATABASE_URL" "$BACKUP_FILE"
echo "Database restore completed; run application acceptance checks before reopening traffic."
