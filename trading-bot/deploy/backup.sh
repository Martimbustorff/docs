#!/usr/bin/env bash
# Back up the bot's runtime state (var/) while the bot keeps running.
#
# The SQLite database is copied with sqlite3's online .backup, which gives a consistent
# snapshot even while the bot writes to it. The rest of var/ (kill switch file, live gate,
# drill result, reports) is copied as is. Each run writes one date-stamped, owner-only
# tarball, then deletes tarballs older than the retention window. Pruning only happens after
# a verified backup, so a failing backup never eats the good ones.
#
# Usage:   deploy/backup.sh
# Env:     BACKUP_DIR        where tarballs go         (default: ~/backups/trading-bot)
#          BACKUP_KEEP_DAYS  retention in days          (default: 14)
#          BOT_VAR_DIR       the bot's var/ directory   (default: <repo>/var)
# Cron:    17 3 * * * /home/trader/trading-bot/deploy/backup.sh 2>&1 | logger -t trading-bot-backup
# Restore: deploy/VPS.md, section "Back up the bot's state".

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
var_dir="${BOT_VAR_DIR:-$repo_dir/var}"
backup_dir="${BACKUP_DIR:-$HOME/backups/trading-bot}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
db="$var_dir/bot.sqlite3"

die() {
  echo "backup: $*" >&2
  exit 1
}

for tool in sqlite3 flock tar gzip; do
  command -v "$tool" >/dev/null || die "$tool not found; install it with: sudo apt install $tool"
done
[[ "$keep_days" =~ ^[0-9]+$ ]] || die "BACKUP_KEEP_DAYS must be a whole number of days, got '$keep_days'"
[[ "$backup_dir" != *"'"* ]] || die "BACKUP_DIR must not contain a single quote"
[[ -f "$db" ]] || die "no database at $db; start the bot once before backing it up"

case "$(realpath -m "$backup_dir")/" in
  "$(realpath -m "$var_dir")/"*) die "BACKUP_DIR must be outside $var_dir, or each backup would contain the last" ;;
esac

umask 077
mkdir -p "$backup_dir"

# One backup at a time. An overlapping run exits quietly instead of racing the first one.
exec 9>"$backup_dir/.lock"
flock -n 9 || { echo "backup: another backup is running; skipping" >&2; exit 0; }

# Holding the lock means any leftover work directory belongs to a run that was killed.
find "$backup_dir" -maxdepth 1 -type d -name '.work-*' -exec rm -rf {} +

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="$(mktemp -d "$backup_dir/.work-$stamp.XXXXXX")"
trap 'rm -rf "$work"' EXIT
snapshot="$work/bot.sqlite3"
tarball="$work/var-$stamp.tar"
final="$backup_dir/var-$stamp.tar.gz"

# Consistent online copy. .timeout waits out the bot's short write locks instead of failing.
sqlite3 "$db" ".timeout 15000" ".backup '$snapshot'"
check="$(sqlite3 "$snapshot" "PRAGMA quick_check;")"
[[ "$check" == "ok" ]] || die "integrity check failed on the snapshot: $check"

# Everything else in var/, minus the live database files the snapshot replaces.
# tar exits 1 when a file changes or vanishes mid-read (e.g. a report being rewritten);
# that is harmless here. Anything above 1 is a real failure.
tar -C "$var_dir" --exclude='bot.sqlite3' --exclude='bot.sqlite3-*' -cf "$tarball" . \
  || { rc=$?; (( rc == 1 )) || die "tar failed with exit code $rc"; }
tar -C "$work" -rf "$tarball" ./bot.sqlite3
gzip -9 "$tarball"
gzip -t "$tarball.gz"
mv "$tarball.gz" "$final"
echo "backup: wrote $final ($(du -h "$final" | cut -f1))"

find "$backup_dir" -maxdepth 1 -type f -name 'var-*.tar.gz' -mtime "+$keep_days" -print -delete \
  | sed 's/^/backup: pruned /'
