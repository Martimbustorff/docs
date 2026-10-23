/** Chamada à API com JSON; devolve os dados ou lança Error com a mensagem em português da API. */
export class ApiClientError extends Error {
  constructor(
    message: string,
    public code: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T = unknown>(url: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: init.method ?? (init.body !== undefined ? "POST" : "GET"),
      headers: init.body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiClientError("Sem ligação à internet. Verifique a rede e tente novamente.", "NETWORK", 0);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiClientError(data?.error?.message ?? "Ocorreu um erro. Tente novamente.", data?.error?.code ?? "ERROR", res.status);
  }
  return data as T;
}

const PT_DATE = (opts: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat("pt-PT", { timeZone: "UTC", ...opts });

/** Formata um dia 'YYYY-MM-DD' sem depender do fuso do dispositivo. */
export function dayParts(date: string) {
  const d = new Date(`${date}T12:00:00Z`);
  return {
    weekday: PT_DATE({ weekday: "short" }).format(d).replace(".", ""),
    day: PT_DATE({ day: "numeric" }).format(d),
    month: PT_DATE({ month: "short" }).format(d).replace(".", ""),
    long: PT_DATE({ weekday: "long", day: "numeric", month: "long" }).format(d),
  };
}

export function shiftDate(date: string, days: number): string {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function mondayOf(date: string): string {
  const wd = new Date(`${date}T12:00:00Z`).getUTCDay(); // 0 = domingo
  return shiftDate(date, -((wd + 6) % 7));
}

export const longLisbon = (iso: string) =>
  new Intl.DateTimeFormat("pt-PT", { dateStyle: "full", timeStyle: "short", timeZone: "Europe/Lisbon" }).format(new Date(iso));
