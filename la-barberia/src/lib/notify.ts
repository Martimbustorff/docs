import { env } from "./env";
import { formatLongPt } from "./time";

export type NotifyEvent = "new" | "cancelled" | "rescheduled";

export interface NotifyBooking {
  customerName: string | null;
  customerPhone: string | null;
  serviceName: string | null;
  startsAt: Date;
  /** Hora anterior (só em remarcações). */
  previousStartsAt?: Date;
}

const TITLE: Record<NotifyEvent, string> = {
  new: "Nova marcação",
  cancelled: "Marcação cancelada pelo cliente",
  rescheduled: "Marcação remarcada pelo cliente",
};

export function buildMessage(event: NotifyEvent, b: NotifyBooking): string {
  const lines = [
    `${TITLE[event]} — La barberia`,
    `Nome: ${b.customerName ?? "—"}`,
    `Telemóvel: ${b.customerPhone ?? "—"}`,
    `${event === "cancelled" ? "Era para" : "Quando"}: ${formatLongPt(b.startsAt)}`,
  ];
  if (event === "rescheduled" && b.previousStartsAt) lines.push(`Antes: ${formatLongPt(b.previousStartsAt)}`);
  if (b.serviceName) lines.push(`Serviço: ${b.serviceName}`);
  return lines.join("\n");
}

/**
 * Avisa o Carlos (por WhatsApp) quando há uma marcação nova, cancelada ou remarcada.
 * Fornecedores (NOTIFY_PROVIDER):
 *  - "callmebot": serviço gratuito e NÃO oficial; o Carlos tem de o ativar uma vez (ver docs/NOTIFICACOES.md).
 *  - "webhook":   POST JSON para NOTIFY_WEBHOOK_URL (ex.: Make, Zapier, n8n) para ligar a outro canal.
 * Nunca lança: uma falha na notificação não pode afetar a marcação (o Carlos vê sempre tudo no painel).
 */
export async function notifyOwner(event: NotifyEvent, booking: NotifyBooking): Promise<"sent" | "skipped" | "failed"> {
  let e: ReturnType<typeof env>;
  try {
    e = env();
  } catch {
    return "skipped";
  }
  if (!e.NOTIFY_PROVIDER) return "skipped";
  const text = buildMessage(event, booking);
  try {
    let res: Response;
    if (e.NOTIFY_PROVIDER === "callmebot") {
      if (!e.CALLMEBOT_PHONE || !e.CALLMEBOT_APIKEY) return "skipped";
      const url = new URL("https://api.callmebot.com/whatsapp.php");
      url.searchParams.set("phone", e.CALLMEBOT_PHONE);
      url.searchParams.set("text", text);
      url.searchParams.set("apikey", e.CALLMEBOT_APIKEY);
      res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    } else if (e.NOTIFY_PROVIDER === "webhook") {
      if (!e.NOTIFY_WEBHOOK_URL) return "skipped";
      res = await fetch(e.NOTIFY_WEBHOOK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event, text, name: booking.customerName, phone: booking.customerPhone, startsAt: booking.startsAt.toISOString() }),
        signal: AbortSignal.timeout(5000),
      });
    } else {
      return "skipped";
    }
    if (!res.ok) {
      // Não registamos o URL (contém a chave) nem o corpo (pode ter dados pessoais).
      console.error("[notify] fornecedor respondeu", e.NOTIFY_PROVIDER, res.status);
      return "failed";
    }
    return "sent";
  } catch (err) {
    console.error("[notify] falha ao notificar", e.NOTIFY_PROVIDER, err instanceof Error ? err.name : "erro");
    return "failed";
  }
}
