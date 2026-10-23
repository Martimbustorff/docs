# La barberia — plataforma de marcações

Aplicação web (adaptada ao telemóvel) para a **La barberia**, do Carlos, em Cascais. Os clientes marcam sem criar conta; o Carlos gere a agenda numa área privada. Português de Portugal, euros, fuso `Europe/Lisbon` (com mudança de hora).

> **Estado:** funcional e testada localmente (ver [docs/TESTES.md](docs/TESTES.md)). **Ainda não está publicada** e depende de configuração externa (ver [Pendente](#pendente-antes-de-publicar)). Os serviços, preços e horários incluídos são **demonstrativos**.

## Arquitetura (e porquê)

| Camada | Escolha | Razão |
|---|---|---|
| Aplicação | Next.js (App Router) + TypeScript | Uma só base de código para site, API e painel; comum e fácil de manter |
| Base de dados | **Postgres comum** (`DATABASE_URL`) | Funciona em Supabase, Neon ou qualquer Postgres; sem dependência de um fornecedor |
| Acesso a dados | `postgres` (driver) com SQL explícito | Poucas dependências; migrações em SQL simples |
| Autenticação | Própria, mínima: scrypt + sessões na BD + cookie `HttpOnly`/`SameSite=Strict` | Sem registo público; sem serviço externo |
| Validação | `zod` | Entrada validada no servidor |
| Datas | `luxon` | Mudança de hora (23 h/25 h) tratada corretamente |

Dependências de execução: `next`, `react`, `react-dom`, `postgres`, `zod`, `luxon`. Sem fontes, scripts ou mapas de terceiros no site.

**Regra de ouro das marcações:** marcações e bloqueios vivem na **mesma tabela** (`calendar_entries`). Uma *restrição de exclusão* do Postgres (`calendar_entries_no_overlap`) impede sobreposições para o mesmo profissional, mesmo com pedidos simultâneos. Além disso, as escritas são serializadas por profissional (lock consultivo) para evitar *deadlocks*. Ver [docs/MODELO-DE-DADOS.md](docs/MODELO-DE-DADOS.md).

## Executar localmente

Requisitos: Node 20+ e um Postgres 14+ (extensão `btree_gist`, incluída por omissão).

```bash
npm install
cp .env.example .env.local          # preencha DATABASE_URL e APP_SECRET (ver comentários)
npm run db:migrate                  # cria as tabelas
npm run db:seed:demo                # OPCIONAL: serviços/horários DEMONSTRATIVOS (só em BD local)
npm run admin:create -- carlos@exemplo.pt   # cria o administrador (pede a palavra-passe sem eco)
npm run dev                         # http://localhost:3000
```

Testes (usam uma base de dados **descartável** cujo nome tem de conter `test`):

```bash
createdb labarberia_test
TEST_DATABASE_URL=postgres://user@localhost:5432/labarberia_test npm test
npm run typecheck
```

## Primeiro administrador (procedimento seguro)

Não existe página de registo. O administrador é criado **a partir de um terminal com acesso à base de dados**:

1. Defina `DATABASE_URL` da base de dados de produção apenas nesse terminal (não a guarde em ficheiros partilhados).
2. `npm run admin:create -- email@exemplo.pt` e escreva a palavra-passe (mín. 12 caracteres) quando pedida — não aparece no ecrã nem fica no histórico.
3. Entre em `/admin/login`, abra **Conta** e, se tiver usado uma palavra-passe provisória, altere-a.
4. Para repor uma palavra-passe esquecida: `npm run admin:create -- email@exemplo.pt --reset` (termina todas as sessões).

## Publicar

Ver [docs/PUBLICACAO.md](docs/PUBLICACAO.md) (alojamento, variáveis de ambiente, domínio, tarefas agendadas) e [docs/SERVICOS-E-LIMITES.md](docs/SERVICOS-E-LIMITES.md) (serviços externos, limites gratuitos e o que pode gerar custos).

## Guias

- [Guia do Carlos](docs/GUIA-DO-CARLOS.md) — gerir a agenda no dia a dia
- [Cópias de segurança e restauro](docs/BACKUPS.md)
- [Modelo de dados](docs/MODELO-DE-DADOS.md)
- [Testes realizados e limitações](docs/TESTES.md)

## Estrutura

```
db/migrations/      Esquema SQL (aplicado por npm run db:migrate)
scripts/            migrate, seed-demo, create-admin, purge, backup.sh, restore.sh
src/lib/            Regras: disponibilidade, marcações, autenticação, ICS, CSV, email
src/app/            Site público, assistente de marcação, link privado, painel /admin
src/app/api/        API pública (/api/public) e privada (/api/admin)
tests/              Testes (vitest) contra Postgres real
```

## Pendente antes de publicar

Ver a lista completa em [docs/TESTES.md](docs/TESTES.md#configuração-pendente). Em resumo: confirmar morada/WhatsApp com o Carlos, preencher os dados do responsável pela privacidade, substituir os dados de demonstração, escolher e configurar alojamento + base de dados, definir `APP_SECRET`/`CRON_SECRET`, agendar a manutenção e as cópias de segurança.
