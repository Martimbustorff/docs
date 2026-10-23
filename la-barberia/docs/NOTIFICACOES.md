# Aviso por WhatsApp ao Carlos

Quando um cliente **marca, cancela ou remarca**, a aplicação pode enviar uma mensagem de WhatsApp ao Carlos com o nome, o telemóvel e a hora. Se o aviso falhar, **a marcação fica na mesma guardada**: o Carlos vê sempre tudo em `/admin` (separador **Próximas**, por ordem cronológica, que atualiza sozinho de minuto a minuto).

## Porque não é "WhatsApp oficial"
A API oficial do WhatsApp para empresas (Meta) exige conta empresarial, número dedicado, modelos de mensagem aprovados e **cobra por mensagem** em muitos casos. Não faz sentido para uma barbearia e não é grátis garantidamente. Por isso a aplicação suporta duas vias simples:

| `NOTIFY_PROVIDER` | O que é | Custo | Riscos |
|---|---|---|---|
| `callmebot` | Serviço de terceiros (CallMeBot) que envia mensagens para o **teu próprio número** | Gratuito, uso pessoal | **Não oficial**: pode mudar ou deixar de funcionar; os dados da marcação (nome, telemóvel, hora) passam por esse serviço (já refletido na página de privacidade) |
| `webhook` | A aplicação faz um POST JSON para um URL teu (Make, Zapier, n8n…) que depois envia para onde quiseres (WhatsApp Business, Telegram, SMS…) | Depende do serviço | Depende do serviço escolhido |

Nunca testei o envio real (preciso do número/chave do Carlos): os testes automáticos verificam o pedido que é feito e que falhas não afetam a marcação.

## Ativar o CallMeBot (uma vez, pelo Carlos, no telemóvel dele)
1. Seguir as instruções oficiais em **callmebot.com** ("WhatsApp API"): o Carlos adiciona o contacto do CallMeBot e envia-lhe a mensagem de autorização indicada no site. Recebe uma **chave (apikey)** por WhatsApp.
2. Definir no alojamento: `NOTIFY_PROVIDER=callmebot`, `CALLMEBOT_PHONE=+351961916045` (o número do Carlos, com +351) e `CALLMEBOT_APIKEY=<a chave>`.
3. Fazer uma marcação de teste e confirmar que a mensagem chega.

A chave é um segredo: só em variáveis de ambiente, nunca no código. A aplicação não a escreve nos registos.

## Exemplo da mensagem
```
Nova marcação — La barberia
Nome: Rui Pereira
Telemóvel: +351912345678
Quando: sexta-feira, 23 de outubro de 2026, 10:30
Serviço: Corte de cabelo
```
