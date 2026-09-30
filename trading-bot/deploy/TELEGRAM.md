# Set up Telegram alerts and approvals

The bot uses a private Telegram bot for three jobs:

- It sends you alerts: fills, errors, kill-switch events and the daily report.
- It asks you to approve any entry order above $1,000.
- It takes a few commands from you, including `/kill`.

This guide takes about 10 minutes. You need the Telegram app and a terminal on your own computer.

> **Treat the bot token like a password.** Anyone with it can read the bot's messages and
> impersonate it. Never paste it into a chat, including an AI assistant, and never commit it.

## Create the bot with BotFather

1. In Telegram, search for **@BotFather** and open the chat. Check that it has the blue verified
   tick. Impostor accounts exist.
2. Send `/newbot`.
3. Enter a display name, for example `My Paper Trader`.
4. Enter a username that ends in `bot`, for example `my_paper_trader_bot`. It must be unique on
   Telegram.
5. BotFather replies with a token that looks like `123456789:AAH...`. Copy it into your password
   manager.

Lock the bot down while you're still talking to BotFather:

1. Send `/setjoingroups`, pick your bot, then choose **Disable**. Nobody can add it to a group.
2. Send `/setcommands`, pick your bot, then send this list. It adds a command menu in the chat.

   ```text
   status - Mode, kill switch, positions and today's P&L
   pnl - Realized and unrealized P&L
   kill - Trip the kill switch and flatten all bot positions
   help - List the commands
   ```

## Put the token in .env

On the server, open `.env` in the repository and set the token. Type or paste it directly into the
editor.

```bash
cd ~/trading-bot
nano .env
```

```text
TELEGRAM_BOT_TOKEN=123456789:AAH...
```

Leave `TELEGRAM_CHAT_ID` empty for now. You'll fill it in two steps from here.

## Send your bot a message

A Telegram bot can't message you until you message it first.

1. Open `https://t.me/<your_bot_username>` on your phone or computer.
2. Tap **Start**.
3. Send any message, for example `hi`.

## Get your chat id

Your chat id tells the bot which chat to trust. You read it from Telegram's `getUpdates` API.

Run these commands **on your own computer**, not on a shared machine and not in any chat. The
token never lands in your shell history, because `read -s` takes it without echoing it.

```bash
printf 'Bot token: '; read -rs TELEGRAM_BOT_TOKEN; echo
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" | python3 -m json.tool
unset TELEGRAM_BOT_TOKEN
```

On Windows, run this in PowerShell 7 instead:

```powershell
$token = Read-Host "Bot token" -MaskInput
(Invoke-RestMethod "https://api.telegram.org/bot$token/getUpdates").result.message.chat
Remove-Variable token
```

Find the message you just sent and read `"chat"` → `"id"`. A private chat has `"type": "private"`
and a positive id, for example `123456789`.

```json
"chat": {
    "id": 123456789,
    "first_name": "Alex",
    "type": "private"
}
```

If `"result"` is an empty list:

- Send the bot another message, then run the command again.
- Stop the bot if it's already running, with `docker compose stop bot`. The bot reads the same
  update queue and consumes your message before `curl` sees it.

Put the id in `.env` on the server:

```text
TELEGRAM_CHAT_ID=123456789
```

## Apply the change

Recreate the containers so they pick up the new `.env`. A plain `docker compose restart` doesn't
reload `.env`.

```bash
docker compose up -d
```

Send `/help` to your bot. It should answer with the command list.

## Commands

Only the chat whose id is in `TELEGRAM_CHAT_ID` can use these.

| Command | What it does |
|---|---|
| `/status` | Shows the trading mode, the kill-switch state, open positions and today's P&L. |
| `/pnl` | Shows realized and unrealized P&L. |
| `/kill` | Trips the kill switch. The bot cancels its open orders, closes every bot position and stops opening new ones. |
| `/help` | Lists the commands. |

There's no `/resume`. You clear the kill switch only on the server, on purpose:

```bash
docker compose exec bot python -m bot resume --confirm
```

A lost or stolen phone can stop the bot, but it can never restart it.

## Approve or reject entries

Any entry order above $1,000 (`risk.approval_threshold_usd` in `strategy.md`) waits for you.

1. The bot sends a message about the trade it wants, with **Approve** and **Reject** buttons.
2. Tap one button. The message updates to show your decision.
3. If you don't answer within 12 hours (`risk.approval_timeout_minutes`), the entry expires and
   the bot doesn't trade it.

An approval doesn't bypass any other check:

- The bot re-prices an approved entry. If the price has moved more than 2%
  (`risk.approval_max_price_drift_pct`) since the signal, it skips the trade.
- An approved entry goes out in the next execution window. That's right away for BTC/USD, and one
  minute after the open for SPY and QQQ.
- The kill switch and the risk limits run again right before the order is sent.

Exits never wait for approval. Stops, strategy exits and kill-switch flattening always run.

## Privacy and security

- **Only your chat id can command the bot.** The bot ignores every update from any other chat
  and logs it. A stranger who finds your bot gets no response.
- Anyone with your unlocked Telegram account can approve pending entries or send `/kill`.
  Set a Telegram passcode and turn on two-step verification in Telegram's privacy settings.
- Bot chats aren't end-to-end encrypted. Telegram's servers can read your alerts, which include
  symbols, sizes and P&L. The bot never sends API keys or passwords over Telegram.
- If your token leaks, send `/revoke` to BotFather and pick your bot. Put the new token in `.env`,
  then run `docker compose up -d`.
- Only one program can poll a bot token at a time. Don't run the bot on your laptop and on the
  server with the same token.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No messages at all | Check that both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. With either one missing, the bot only logs alerts to the console. Then run `docker compose up -d`. |
| The bot ignores your commands | `TELEGRAM_CHAT_ID` doesn't match your chat. Run `docker compose logs bot \| grep -i telegram` to see which chat the bot ignored, then repeat [Get your chat id](#get-your-chat-id). |
| `401 Unauthorized` in the logs | The token is wrong or was revoked. Copy it again from BotFather. |
| `409 Conflict` in the logs | Another process polls the same token, or a webhook is set. Stop the other process. To clear a webhook, read the token with `read -rs` as in [Get your chat id](#get-your-chat-id), then run `curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"` on your own computer. |
| Approvals expire before you see them | Check that Telegram notifications for the bot chat aren't muted. |
