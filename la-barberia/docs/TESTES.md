# Testes realizados, limitações e configuração pendente

Ambiente de teste (atualizado após as alterações pedidas pelo Carlos: marcações de 30 min, sem preços nem email, lista cronológica, aviso por WhatsApp): Node 22, PostgreSQL 16 local, Chromium (Playwright) com viewport de telemóvel (390×844) e fuso do dispositivo deliberadamente diferente (`America/New_York`). Data dos testes: 23/10/2026.

## Resultados

### Automáticos — `npm test` (62 testes, repetidos várias vezes sem falhas intermitentes) e `npm run typecheck`
Correm contra Postgres real (não há *mocks* da base de dados).

| Requisito | Como foi verificado |
|---|---|
| Criar e consultar uma marcação | Marcação guardada na BD; link privado mostra o resumo sem expor o telemóvel |
| Cancelar e remarcar pelo link | Cancelar liberta a vaga e é idempotente; remarcar move a mesma linha; prazo de cancelamento respeitado; máximo de 3 remarcações |
| **Sem reservas concorrentes** | 20 pedidos simultâneos para a mesma vaga → exatamente 1 aceite; pedidos simultâneos com vagas sobrepostas parcialmente → nunca há sobreposição; remarcações simultâneas para a mesma vaga → 1 vence e a outra mantém a reserva; "sem preferência" com 2 profissionais distribui e esgota; a restrição da BD rejeita sobreposições mesmo por SQL direto |
| Repetições / cliques duplos | Mesma chave de idempotência (também 8 em simultâneo) → 1 marcação e o mesmo link; chave reutilizada para outra marcação → recusada |
| Remarcação atómica | Remarcar para vaga ocupada falha e a reserva original fica intacta (também no painel) |
| Pausas, bloqueios, duração | Horas na pausa de almoço, fora do horário, que ultrapassam o fecho ou desalinhadas são recusadas; bloqueios removem vagas; bloqueio sobre marcação existente é recusado; serviço de 45 min só aparece onde cabe |
| Antecedência e horizonte | Recusa marcações dentro da antecedência mínima e além do horizonte |
| **Fuso e mudança de hora** | 09:30 locais = 08:30Z no verão e 09:30Z no inverno; dia de 25 h (25/10/2026) e de 23 h (29/03/2026) com o nº correto de vagas; hora inexistente na mudança de hora rejeitada; o servidor devolve as etiquetas de hora, por isso o fuso do dispositivo não interfere |
| **Acesso administrativo** | Todas as rotas `/api/admin/*` (descobertas automaticamente no disco) devolvem 401 sem sessão; sessão inventada/expirada recusada; pedidos que alteram dados com `Origin` externo ou em falta → 403 (CSRF); cookie `HttpOnly` + `SameSite=Strict`; resposta de login idêntica para email inexistente e palavra-passe errada; bloqueio após 5 falhas; limite de tentativas por IP; mudar a palavra-passe termina outras sessões; não existe rota de registo |
| **Disponibilidade sem dados pessoais** | A resposta pública não contém nomes, telefones, emails nem ids de profissionais; a listagem do painel não devolve o hash do token nem a chave de idempotência |
| Abuso | Campo isco (honeypot), limite por IP, limite de marcações ativas por telemóvel, marcações fechadas nas definições, validação (telemóvel inválido, JSON inválido, `Content-Type` errado) com mensagens em português |
| Privacidade/retenção | A limpeza apaga marcações além do prazo e mantém as futuras; segredo do endpoint de manutenção validado |
| CSV/ICS | Neutralização de fórmulas (`=`, `+`, `-`, `@`), separador `;`, BOM UTF-8; ICS em UTC com CRLF e escapes |

### Ponta a ponta no navegador (35 verificações, `tests/e2e/booking-journey.mjs`)
Percurso completo em telemóvel: página inicial → escolher serviço → vagas reais → dados → rever → **duplo clique em "Confirmar" (criou 1 só marcação)** → página privada → `.ics` → login no painel → a marcação aparece na agenda (dia e semana) → o painel recusa uma sobreposição → **cancelamento pelo link seguro** guardado na BD → reflexo na agenda. Inclui auditoria **axe-core (WCAG 2.1 AA)** em home, escolha de hora, confirmação, agenda, equipa, serviços, definições e privacidade (0 violações; encontrou e corrigi um problema de contraste), ausência de scroll horizontal, e ausência de erros inesperados na consola. Este *script* é específico do ambiente de desenvolvimento e não faz parte de `npm test`.

### Cópias de segurança
`scripts/backup.sh` → `scripts/restore.sh` testados: base restaurada com as mesmas contagens em todas as tabelas e com a restrição anti-sobreposição presente.

## O que **não** foi testado
- Alojamento real, Supabase/Neon reais, *poolers* de ligações (a aplicação usa `prepare: false` para os suportar, mas só testei Postgres 16 local).
- Envio real do aviso por WhatsApp (CallMeBot): os testes verificam o pedido feito e que falhas não afetam a marcação, mas **nunca enviei uma mensagem real** (falta a chave do Carlos).
- Dispositivos reais (iOS Safari, Android) e leitores de ecrã: só Chromium emulado + auditoria automática. Recomenda-se um teste manual em 2 telemóveis antes de abrir ao público.
- Carga/desempenho; comportamento atrás de um CDN/proxy concreto.
- Os termos e limites dos serviços gratuitos (não consegui abrir as páginas oficiais; ver [SERVICOS-E-LIMITES.md](SERVICOS-E-LIMITES.md)).

## Limitações conhecidas
- Todos os profissionais fazem todos os serviços (sem matriz serviço × profissional).
- Um só nível de administrador; sem recuperação de palavra-passe por email (usa-se `admin:create --reset` com acesso à BD).
- Quem perder o link privado tem de contactar a barbearia (o link não pode ser recuperado: só se guarda o hash). Se o cliente indicar email e o envio estiver ativo, recebe o link por email.
- Um atacante que conheça o email do Carlos pode bloquear o início de sessão dele durante 15 min com 5 tentativas falhadas (compromisso aceite contra força bruta; o limite por IP atenua).
- A sessão do painel dura 7 dias; em computadores partilhados use **Sair**.
- Não há lembretes, pagamentos, SMS nem confirmações automáticas por WhatsApp (por desenho). O WhatsApp é contacto manual.
- A página de privacidade é um texto-modelo; deve ser revista antes de publicar.
- Mudanças no horário semanal não revalidam marcações já existentes.

## Configuração pendente
1. **Confirmar com o Carlos:** morada, WhatsApp (+351 961 916 045, retirado do Instagram), slogan e Instagram.
2. **Dados reais:** horário semanal e pausas; regras (antecedência, prazo de cancelamento, horizonte); desligar o modo de demonstração.
3. **Privacidade:** nome/firma, NIF e email do responsável; rever o texto.
4. **Infraestrutura:** escolher alojamento e BD (verificando uso comercial gratuito), `DATABASE_URL`, `APP_SECRET`, `PUBLIC_BASE_URL`, `CRON_SECRET`; `npm run db:migrate`; criar o administrador.
5. **Rotinas:** agendar a tarefa diária de manutenção e as cópias de segurança; testar um restauro.
6. **Aviso por WhatsApp ao Carlos:** ativar o CallMeBot e configurar as variáveis ([NOTIFICACOES.md](NOTIFICACOES.md)). Opcional: domínio próprio.
7. **Fotografias/logótipo:** não foram usadas imagens sem autorização; o ícone é um "L" provisório. O logótipo do Instagram só deve ser usado com autorização do Carlos.
