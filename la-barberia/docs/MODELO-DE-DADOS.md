# Modelo de dados

Ficheiro de origem: [`db/migrations/0001_init.sql`](../db/migrations/0001_init.sql). Aplicar com `npm run db:migrate` (regista o que já foi aplicado em `schema_migrations`; cada migração corre numa transação; um *advisory lock* evita execuções em simultâneo).

| Tabela | Para quê |
|---|---|
| `settings` | Uma linha: dados da barbearia, regras (antecedência, horizonte, prazo de cancelamento, passo das vagas, conservação), `demo_mode`, `contact_confirmed`, dados do responsável pela privacidade |
| `staff` | Profissionais (o Carlos; outros se existirem) |
| `services` | Serviços: duração (sem preços), `is_demo`. Por omissão um só: "Corte de cabelo", 30 min |
| `working_hours` | Horário semanal por profissional. Várias linhas por dia = **pausas** nos intervalos. Intervalos sobrepostos são rejeitados |
| `calendar_entries` | **Marcações e bloqueios** (`kind = appointment | block`). Restrição `calendar_entries_no_overlap` (`EXCLUDE USING gist`) por profissional, ignorando `cancelled` |
| `admins`, `admin_sessions` | Administradores (hash scrypt) e sessões (só o hash do token) |
| `rate_limits` | Limitação de pedidos; a chave contém apenas um HMAC do IP |

Decisões relevantes:

- Horas em `timestamptz` (UTC). A conversão para `Europe/Lisbon` faz-se na aplicação; o servidor devolve já as etiquetas de hora, por isso o fuso do dispositivo do cliente é irrelevante.
- A marcação guarda uma **cópia** do nome e da duração do serviço no momento da marcação. Não se guardam preços nem emails de clientes (removidos na migração 0002).
- O link privado é um token aleatório; só o **hash SHA-256** fica na base de dados. Nas marcações online, o token deriva (HMAC com `APP_SECRET`) da chave de idempotência do pedido, para que repetir um pedido devolva o mesmo link sem criar duplicados.
- Bloqueios para "toda a barbearia" criam uma linha por profissional ativo, agrupadas por `block_group`.
- RLS ativado, sem políticas, em todas as tabelas: se usar Supabase, a API pública do Supabase não consegue ler nada; só o servidor da aplicação (ligação direta) acede.
- Remarcar = `UPDATE` da mesma linha numa transação. Se o novo horário falhar, a linha original fica intacta.
