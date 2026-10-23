# Cópias de segurança e restauro

Os planos gratuitos de Postgres gerido podem não incluir cópias automáticas descarregáveis (confirme no seu fornecedor). Por isso a aplicação traz scripts próprios, **testados** (cópia de uma base e restauro noutra, com contagens iguais e a restrição anti-sobreposição restaurada).

Requisito: cliente PostgreSQL (`pg_dump`, `pg_restore`) com versão igual ou superior à do servidor.

## Fazer uma cópia
```bash
DATABASE_URL="postgres://…" ./scripts/backup.sh backups
```
Gera `backups/labarberia-AAAA-MM-DD_HHMM.dump` (permissões 600). Contém **dados pessoais**: guarde-o cifrado (`gpg --symmetric ficheiro.dump`) e fora do repositório (`backups/` está no `.gitignore`).

**Frequência sugerida:** semanal (e antes de qualquer migração ou alteração grande). Pode agendar com GitHub Actions: ver [backup-workflow.example.yml](backup-workflow.example.yml). Guarde as cópias por tempo limitado (acompanhe o prazo de conservação definido na privacidade).

## Restaurar
1. Crie uma base de dados **vazia** de destino.
2. `TARGET_DATABASE_URL="postgres://…" ./scripts/restore.sh backups/ficheiro.dump`
3. Verifique: `psql "$TARGET_DATABASE_URL" -c "select count(*) from calendar_entries"`.
4. Aponte `DATABASE_URL` da aplicação para a nova base de dados e reinicie.

**Teste o restauro antes de precisar dele** (por exemplo, trimestralmente, numa base de dados temporária).

## Recuperação de desastre (resumo)
Base perdida/corrompida → criar nova BD → restaurar a última cópia → atualizar `DATABASE_URL` → `npm run db:migrate` (aplica migrações em falta) → verificar a agenda. Marcações feitas depois da última cópia perdem-se; por isso clientes que tenham o link podem ter marcações que já não existem — contacte-os.

Se mantiver o mesmo `APP_SECRET`, os links privados das marcações restauradas continuam a funcionar (o hash do token está na própria base de dados).
