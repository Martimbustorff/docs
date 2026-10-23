# Publicação

A aplicação é um servidor Node.js (`npm run build` + `npm start`) que precisa de um Postgres. **Não publiquei nem contratei nada.** Esta página explica o que fazer e o que verificar primeiro.

## 1. Escolher alojamento — atenção à licença "gratuita"

- **Vercel, plano Hobby: não serve.** Os termos da Vercel limitam o plano Hobby a uso pessoal e **não comercial**. Uma barbearia é uso comercial; exigiria o plano Pro (pago).
- Alternativas a **verificar antes de usar** (não consegui abrir as páginas oficiais a partir deste ambiente; confirme preços, limites e a permissão de uso comercial no próprio fornecedor, na data da contratação):
  - **Netlify** (plano gratuito, Next.js suportado), **Render** (serviço web gratuito; costuma "adormecer" após inatividade, o que atrasa a primeira visita) ou outro serviço que corra Node.js.
  - **Cloudflare Workers/Pages** exige adaptação (OpenNext); o plano gratuito limita o CPU por pedido (10 ms segundo a documentação da Cloudflare), o que pode ser apertado para esta aplicação. Não testei.
  - Um VPS pequeno (pago) é a opção mais previsível se o gratuito não servir.
- A aplicação não depende de nenhum fornecedor: só precisa de Node 20+, `DATABASE_URL` e das variáveis abaixo.

## 2. Base de dados

Qualquer Postgres 14+ com a extensão `btree_gist` (Supabase e Neon incluem-na).

1. Crie o projeto/base de dados e copie a *connection string* (`postgres://…`, normalmente com `?sslmode=require`).
2. No seu computador: `DATABASE_URL="…" npm run db:migrate`.
3. **Não** corra `db:seed:demo` em produção (recusa bases remotas sem `--allow-remote`). Crie o profissional, serviços e horário reais no painel (ou use o seed só para ter um ponto de partida editável e depois desligue o modo de demonstração).
4. Crie o administrador: ver [README](../README.md#primeiro-administrador-procedimento-seguro).

Supabase: a aplicação liga-se diretamente ao Postgres; as tabelas têm RLS ativo sem políticas, por isso a API pública do Supabase não expõe dados. Não use as chaves `anon`/`service_role` no código — esta aplicação não as usa.

## 3. Variáveis de ambiente (no painel do alojamento)

| Variável | Obrigatória | Notas |
|---|---|---|
| `DATABASE_URL` | sim | String de ligação |
| `APP_SECRET` | sim | ≥ 32 caracteres aleatórios (`openssl rand -base64 48`). Se mudar, os links privados antigos deixam de funcionar |
| `PUBLIC_BASE_URL` | sim | URL final, sem `/` no fim |
| `CRON_SECRET` | recomendada | Protege `/api/cron/maintenance` |
| `DB_POOL_MAX` | não | Use 1–3 em alojamento *serverless* |
| `RESEND_API_KEY`, `EMAIL_FROM` | não | Email opcional; sem isto não se enviam emails |

Nunca faça commit de `.env.local`. O repositório só tem `.env.example`.

## 4. Atrás de um proxy/CDN

Os limites de pedidos usam o primeiro IP de `X-Forwarded-For`. Em fornecedores comuns isto é preenchido pelo próprio fornecedor. Se o seu alojamento deixar o cliente escolher esse cabeçalho, o limite por IP pode ser contornado (a restrição de exclusão, a validação no servidor e o limite por telemóvel continuam a proteger as marcações).

## 5. Tarefa diária (limpeza + manter a BD ativa)

Chame `POST https://o-seu-site/api/cron/maintenance` com `Authorization: Bearer <CRON_SECRET>` uma vez por dia. Apaga marcações além do prazo de conservação, sessões expiradas e contadores antigos, e conta como atividade para planos gratuitos que pausam bases de dados inativas. Exemplo com GitHub Actions (`.github/workflows/maintenance.yml` do repositório que alojar o código):

```yaml
name: manutencao
on:
  schedule: [{ cron: "17 3 * * *" }]
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - run: curl -fsS -X POST -H "Authorization: Bearer ${{ secrets.CRON_SECRET }}" "${{ secrets.SITE_URL }}/api/cron/maintenance"
```

`/api/health` responde `{"ok":true}` se a base de dados estiver acessível (útil para monitorização gratuita).

## 6. Endereço

Comece com o subdomínio gratuito do alojamento. Um domínio próprio é opcional e **pago anualmente**.

## 7. Lista antes de abrir ao público

- [ ] Morada e WhatsApp confirmados com o Carlos (Definições → "Confirmo…")
- [ ] Nome/firma, NIF e email de privacidade preenchidos; texto da página de privacidade revisto
- [ ] Serviços, preços, durações e horários reais; modo de demonstração desligado
- [ ] Regras (antecedência, prazo de cancelamento, horizonte) acordadas com o Carlos
- [ ] `APP_SECRET`/`CRON_SECRET` definidos; administrador criado e palavra-passe alterada
- [ ] Tarefa diária agendada; cópia de segurança agendada e **restauro testado** ([BACKUPS.md](BACKUPS.md))
- [ ] Fez uma marcação de teste real, viu-a na agenda e cancelou-a pelo link
- [ ] Verificados os termos do plano gratuito escolhido para uso comercial
