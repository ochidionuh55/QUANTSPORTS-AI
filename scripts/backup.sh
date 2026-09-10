#!/bin/sh
# Daily PostgreSQL backup with retention.
#
# Runs as its own container so a backup does not depend on anyone being logged
# in. Dumps are compressed, timestamped, and pruned after RETENTION_DAYS.
#
# Restore with:
#   gunzip -c backups/quantsport-YYYY-MM-DD.sql.gz \
#     | docker compose -f docker-compose.prod.yml exec -T postgres \
#         psql -U quantsport -d quantsport

set -eu

HOST="${POSTGRES__HOST:-postgres}"
USER="${POSTGRES__USER:-quantsport}"
DATABASE="${POSTGRES__DATABASE:-quantsport}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-86400}"

echo "backup: every ${INTERVAL_SECONDS}s, keeping ${RETENTION_DAYS} days"

while true; do
    STAMP=$(date -u +%Y-%m-%d)
    TARGET="/backups/quantsport-${STAMP}.sql.gz"

    if pg_dump -h "$HOST" -U "$USER" -d "$DATABASE" --no-owner --clean \
        | gzip > "${TARGET}.partial"; then
        # Written to .partial first: an interrupted dump must never replace a
        # good backup with a truncated one.
        mv "${TARGET}.partial" "$TARGET"
        echo "backup: wrote $TARGET ($(du -h "$TARGET" | cut -f1))"
    else
        echo "backup: FAILED at $(date -u +%FT%TZ)" >&2
        rm -f "${TARGET}.partial"
    fi

    find /backups -name 'quantsport-*.sql.gz' -mtime "+${RETENTION_DAYS}" -delete
    sleep "$INTERVAL_SECONDS"
done
