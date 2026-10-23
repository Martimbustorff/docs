# Guia do Carlos — gerir a agenda

Abra o site e vá a `/admin` (por exemplo `https://o-seu-site/admin`). Entre com o seu email e palavra-passe. No telemóvel funciona como no computador.

## Agenda
- **Próximas** (vista inicial): todas as marcações futuras **por ordem cronológica**, agrupadas por dia (Hoje, Amanhã, …). Atualiza sozinha de minuto a minuto.
- **Dia / Semana:** para ver um dia ou uma semana concreta; use **Anterior / Hoje / Seguinte** ou a data.
- Cada marcação mostra hora, estado, cliente, serviço, telefone e origem (online, telefone, WhatsApp, presencial). Toque no telefone para ligar ou em **WhatsApp** para escrever.
- **Concluída:** depois do atendimento. **Falta:** se o cliente não apareceu. **Cancelar** liberta a vaga. **Remarcar:** escolha novo dia/hora; se a hora estiver ocupada, nada muda. **Nota:** apontamento interno.
- **Mostrar canceladas** (ou **Repor como confirmada**, se cancelou sem querer e a hora ainda estiver livre).

## Marcação por telefone, WhatsApp ou presencial
Em **Nova marcação**: serviço, dia, hora, nome, telefone → **Criar marcação**. Pode marcar fora do horário habitual; o sistema só recusa se se sobrepuser a outra marcação ou bloqueio. Aparece um **link privado** (só uma vez): pode copiá-lo e enviá-lo ao cliente por WhatsApp para ele ver/cancelar.

## Férias, feriados, pausas pontuais
Em **Bloquear horário**: tipo, dias, (hora, ou "dia inteiro") → **Criar bloqueio**. Se já houver marcações nesse período, o sistema diz quais; cancele ou remarque-as primeiro. Para remover, use **Remover** no cartão do bloqueio.

## Serviços e durações
Por omissão há um só serviço, **Corte de cabelo (30 minutos)**, e o cliente nem chega a escolher serviço: escolhe o dia e a hora. Em **Serviços** pode alterar o nome/duração ou acrescentar outros (aí o cliente passa a escolher). Não há preços no sistema.

## Horário semanal e pausas
Menu **Equipa e horários**. Para cada dia, adicione períodos (ex.: 09:30–13:00 **e** 14:30–19:00 — o intervalo é a pausa de almoço). Sem períodos = fechado. Alterar o horário **não** mexe nas marcações já feitas.

## Definições
Menu **Definições**: morada, WhatsApp, regras (antecedência mínima, até quantos dias à frente se pode marcar, prazo para o cliente cancelar online, intervalo entre horas — por omissão 30 minutos —, meses de conservação) e dados do responsável (necessários para a página de privacidade). Quando os serviços e horários forem os reais, **desligue o modo de demonstração**.

## Exportar
Menu **Conta → Exportar**: escolha as datas e descarregue o CSV (abre no Excel). Tem dados pessoais: guarde em local seguro e apague quando já não precisar.

## Segurança
- Use uma palavra-passe longa e única (mín. 12 caracteres) e **Sair** em computadores partilhados.
- Depois de 5 tentativas falhadas, a conta bloqueia 15 minutos.
- Alterar a palavra-passe termina as sessões noutros aparelhos.
- Nunca partilhe links privados de clientes publicamente.

## O que o cliente vê
O cliente escolhe dia e hora, indica nome e telemóvel e confirma; vê a confirmação **no ecrã** e guarda o link. Não vê preços. O botão de WhatsApp do site é só para conversar consigo: **o cliente não recebe confirmações automáticas por WhatsApp nem SMS**. Quem recebe aviso automático é **o Carlos** (ver [NOTIFICACOES.md](NOTIFICACOES.md)). O cliente pode cancelar/remarcar online até ao prazo definido (por omissão 4 h antes); depois disso é remetido para o WhatsApp.
