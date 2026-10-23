#!/usr/bin/env bash
# Restaura uma cópia para uma base de dados VAZIA (ou apaga e recria as tabelas com --clean).
# Uso: TARGET_DATABASE_URL=... ./scripts/restore.sh ficheiro.dump
# Teste primeiro numa base de dados nova antes de restaurar em produção.
set -euo pipefail
: "${TARGET_DATABASE_URL:?Defina TARGET_DATABASE_URL (base de dados de destino)}"
FILE="${1:?Indique o ficheiro .dump}"
pg_restore --no-owner --no-privileges --clean --if-exists --dbname="$TARGET_DATABASE_URL" "$FILE"
echo "Restauro concluído. Verifique com: psql \"\$TARGET_DATABASE_URL\" -c 'select count(*) from calendar_entries'"
