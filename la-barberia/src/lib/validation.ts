import { z } from "zod";
import { DATE_RE, LOCAL_DT_RE } from "./time";

/** Normaliza telemóveis portugueses (com ou sem +351) e números internacionais para E.164. Devolve null se inválido. */
export function normalizePhone(input: string): string | null {
  const s = input.replace(/[\s().-]/g, "");
  let m = /^(?:\+|00)351(\d{9})$/.exec(s) ?? /^(\d{9})$/.exec(s);
  if (m) {
    const n = m[1];
    return /^[239]\d{8}$/.test(n) ? `+351${n}` : null;
  }
  m = /^(?:\+|00)([1-9]\d{7,14})$/.exec(s);
  return m ? `+${m[1]}` : null;
}

const name = z
  .string()
  .trim()
  .min(2, "Indique o seu nome.")
  .max(80)
  // sem caracteres de controlo
  .refine((v) => !/[\u0000-\u001f\u007f]/.test(v), "Nome inválido.");

const phone = z
  .string()
  .trim()
  .max(30)
  .transform((v, ctx) => {
    const n = normalizePhone(v);
    if (!n) {
      ctx.addIssue({ code: "custom", message: "Indique um número de telemóvel válido." });
      return z.NEVER;
    }
    return n;
  });

const optionalEmail = z
  .string()
  .trim()
  .max(120)
  .optional()
  .transform((v) => (v ? v : undefined))
  .pipe(z.email("Email inválido.").optional());

export const idempotencyKey = z.string().regex(/^[A-Za-z0-9_-]{22,64}$/, "Chave inválida.");

export const publicBookingInput = z.object({
  serviceId: z.uuid(),
  staffId: z.union([z.uuid(), z.literal("any")]),
  startsAt: z.iso.datetime({ offset: true }),
  name,
  phone,
  email: optionalEmail,
  idempotencyKey,
  /** Campo isco (honeypot): tem de vir vazio. Os robôs costumam preenchê-lo. */
  website: z.string().max(200).optional(),
});
export type PublicBookingInput = z.infer<typeof publicBookingInput>;

export const rescheduleInput = z.object({ startsAt: z.iso.datetime({ offset: true }) });

export const availabilityQuery = z.object({
  serviceId: z.uuid(),
  staffId: z.union([z.uuid(), z.literal("any")]),
  from: z.string().regex(DATE_RE),
  to: z.string().regex(DATE_RE),
});

const localDateTime = z.string().regex(LOCAL_DT_RE, "Data/hora inválida.");

export const adminAppointmentInput = z.object({
  serviceId: z.uuid(),
  staffId: z.uuid(),
  start: localDateTime,
  name,
  phone,
  email: optionalEmail,
  notes: z.string().trim().max(500).optional(),
  source: z.enum(["phone", "whatsapp", "walk_in", "online"]).default("phone"),
});

export const adminAppointmentPatch = z
  .object({
    status: z.enum(["confirmed", "completed", "no_show", "cancelled"]).optional(),
    start: localDateTime.optional(),
    staffId: z.uuid().optional(),
    notes: z.string().trim().max(500).optional(),
  })
  .refine((v) => Object.keys(v).length > 0, "Sem alterações.");

export const blockInput = z.object({
  staffId: z.union([z.uuid(), z.literal("all")]),
  start: localDateTime,
  end: localDateTime,
  kind: z.enum(["block", "vacation", "holiday"]),
  reason: z.string().trim().max(120).optional(),
});

export const serviceInput = z.object({
  name: z.string().trim().min(1).max(80),
  description: z.string().trim().max(300).default(""),
  priceCents: z.number().int().min(0).max(100000),
  durationMin: z.number().int().min(5).max(480),
  active: z.boolean().default(true),
  sortOrder: z.number().int().min(0).max(1000).default(0),
});

export const staffInput = z.object({
  name: z.string().trim().min(1).max(60),
  active: z.boolean().default(true),
  sortOrder: z.number().int().min(0).max(1000).default(0),
});

const hhmm = z.string().regex(/^([01]\d|2[0-3]):[0-5]\d$/);
export const workingHoursInput = z.object({
  intervals: z
    .array(z.object({ weekday: z.number().int().min(1).max(7), start: hhmm, end: hhmm }))
    .max(70)
    .refine((a) => a.every((i) => i.end > i.start), "A hora de fim tem de ser posterior à de início."),
});

export const settingsInput = z.object({
  shopName: z.string().trim().min(1).max(80),
  tagline: z.string().trim().max(120),
  address: z.string().trim().min(1).max(200),
  whatsappE164: z
    .string()
    .trim()
    .transform((v, ctx) => {
      const n = normalizePhone(v);
      if (!n) {
        ctx.addIssue({ code: "custom", message: "Número de WhatsApp inválido." });
        return z.NEVER;
      }
      return n;
    }),
  instagram: z.string().trim().max(60).regex(/^[A-Za-z0-9._]*$/, "Utilizador de Instagram inválido (sem @)."),
  legalName: z.string().trim().max(120),
  legalNif: z.string().trim().max(20),
  privacyEmail: z.string().trim().max(120),
  contactConfirmed: z.boolean(),
  demoMode: z.boolean(),
  onlineBookingOpen: z.boolean(),
  minNoticeMinutes: z.number().int().min(0).max(10080),
  horizonDays: z.number().int().min(1).max(365),
  cancelDeadlineHours: z.number().int().min(0).max(168),
  slotStepMinutes: z.union([z.literal(5), z.literal(10), z.literal(15), z.literal(20), z.literal(30), z.literal(60)]),
  retentionMonths: z.number().int().min(1).max(120),
});

export const loginInput = z.object({ email: z.string().trim().toLowerCase().max(120), password: z.string().min(1).max(200) });
export const passwordChangeInput = z.object({ current: z.string().min(1).max(200), next: z.string().min(12, "Mínimo de 12 caracteres.").max(200) });
