import { env } from "./env";
import { formatEuro, formatLongPt } from "./time";
import type { EntryRow, Settings } from "./types";

/** Email opcional via Resend (API HTTP, sem dependências). Nunca lança: uma falha não pode afetar a marcação. */
export async function sendBookingEmail(entry: EntryRow, token: string, settings: Settings): Promise<"sent" | "skipped" | "failed"> {
  const e = env();
  if (!e.RESEND_API_KEY || !e.EMAIL_FROM || !entry.customerEmail) return "skipped";
  const link = `${e.PUBLIC_BASE_URL}/marcacao/${token}`;
  const when = formatLongPt(entry.startsAt);
  const text = [
    `Olá ${entry.customerName},`,
    "",
    `A sua marcação na ${settings.shopName} está registada:`,
    `${entry.serviceName} (${formatEuro(entry.priceCents ?? 0)}) — ${when}`,
    settings.address,
    "",
    `Para ver, remarcar ou cancelar: ${link}`,
    "",
    "Guarde este email: o link é pessoal e é a forma de gerir a marcação online.",
  ].join("\n");
  try {
    const res = await fetch("https://api.resend.com/emails", {
      method: "POST",
      headers: { Authorization: `Bearer ${e.RESEND_API_KEY}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        from: e.EMAIL_FROM,
        to: [entry.customerEmail],
        subject: `Marcação confirmada — ${settings.shopName}`,
        text,
      }),
      signal: AbortSignal.timeout(4000),
    });
    if (!res.ok) {
      console.error("[email] fornecedor respondeu", res.status);
      return "failed";
    }
    return "sent";
  } catch (err) {
    console.error("[email] falha ao enviar", err instanceof Error ? err.message : err);
    return "failed";
  }
}
