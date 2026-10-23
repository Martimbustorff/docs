# Serviços externos, limites e custos possíveis

Não prometo custo zero para sempre: os planos gratuitos mudam. O que segue reflete o que consegui confirmar a 23/10/2026 e o que **tem de ser verificado** antes de contratar.

| Serviço | Para quê | Limites gratuitos conhecidos | Estado da verificação |
|---|---|---|---|
| **Postgres gerido** (Supabase ou Neon) | Guardar marcações | Supabase (plano Free): pausa projetos com pouca atividade durante 7 dias; 500 MB por projeto e 2 projetos ativos, segundo um guia de terceiros de jul/2026 | Pausa confirmada na documentação do Supabase via pesquisa; números a confirmar na página de preços. Termos de uso comercial: **verificar** |
| **Alojamento Node.js** | Correr o site | Depende do fornecedor (ver [PUBLICACAO.md](PUBLICACAO.md)) | **Vercel Hobby é não comercial** (confirmado nos termos). Outros: **por verificar** |
| **Aviso por WhatsApp ao Carlos (opcional)** | Avisar o Carlos de cada marcação | CallMeBot: gratuito, uso pessoal, **não oficial** (ver [NOTIFICACOES.md](NOTIFICACOES.md)) | Não testado com envio real |
| **Tarefas agendadas** | Limpeza diária | GitHub Actions tem minutos gratuitos; alternativa: um serviço de cron gratuito | **Por verificar** |
| **Domínio próprio** | Endereço bonito | — (pago, renovação anual) | Opcional |

## Situações que podem gerar custos
- Plano gratuito de alojamento/BD **não permitir uso comercial** ou ser reduzido → passar a um plano pago.
- A BD gratuita **pausar** por inatividade (mitigado pela tarefa diária) ou precisar de plano pago para cópias de segurança automáticas/mais espaço. *Estimativa* de ocupação: cada marcação ocupa ~1–2 KB; mesmo com milhares de marcações por ano são dezenas de MB, muito abaixo de 500 MB.
- Tráfego/execuções acima da quota do alojamento (improvável numa barbearia, mas possível com ataques de robôs; há limites de pedidos).
- API oficial de WhatsApp/SMS (não incluídos de propósito) ou outro serviço de avisos pago.
- Armazenamento das cópias de segurança fora do plano gratuito.
- Domínio próprio e, mais tarde, pagamentos online (não incluídos).

## O que **não** foi incluído de propósito
Pagamentos online, SMS/WhatsApp automáticos, lembretes, conta de cliente, marketing.
