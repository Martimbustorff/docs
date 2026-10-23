#!/usr/bin/env bash
# Cópia de segurança completa (formato custom do pg_dump). Uso: DATABASE_URL=... ./scripts/backup.sh [pasta]
# O ficheiro contém dados pessoais de clientes: guarde-o cifrado ou em local privado.
set -euo pipefail
: "${DATABASE_URL:?Defina DATABASE_URL}"
DIR="${1:-backups}"
mkdir -p "$DIR"
FILE="$DIR/labarberia-$(date +%Y-%m-%d_%H%M).dump"
pg_dump "$DATABASE_URL" --format=custom --no-owner --no-privileges --file="$FILE"
chmod 600 "$FILE"
echo "Cópia criada: $FILE ($(du -h "$FILE" | cut -f1))"
