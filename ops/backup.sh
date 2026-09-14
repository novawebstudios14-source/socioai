#!/usr/bin/env sh
set -eu
: "${DATABASE_URL:?DATABASE_URL is required}"
: "${BACKUP_DIR:?BACKUP_DIR is required}"
case "$BACKUP_DIR" in /|/var|/home|/root) echo "BACKUP_DIR is too broad" >&2; exit 2;; esac
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
pg_dump --format=custom --no-owner --file="$BACKUP_DIR/database-$stamp.dump" "$DATABASE_URL"
if [ -n "${DATA_DIR:-}" ] && [ -d "$DATA_DIR" ]; then
  tar -czf "$BACKUP_DIR/files-$stamp.tar.gz" -C "$DATA_DIR" .
fi
find "$BACKUP_DIR" -type f -mtime "+${BACKUP_RETENTION_DAYS:-30}" -delete
echo "Backup completed: $stamp"
