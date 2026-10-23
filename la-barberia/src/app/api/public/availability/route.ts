import { db } from "@/lib/db";
import { publicAvailability } from "@/lib/bookings";
import { clientIpHash, route } from "@/lib/http";
import { rateLimit } from "@/lib/ratelimit";
import { availabilityQuery } from "@/lib/validation";
import { Errors } from "@/lib/errors";
import { isValidDate } from "@/lib/time";

export const dynamic = "force-dynamic";

/** Vagas livres (apenas horas). Nunca devolve nomes, telefones ou identificadores de profissionais. */
export const GET = route(async (req) => {
  const q = availabilityQuery.parse(Object.fromEntries(req.nextUrl.searchParams));
  if (!isValidDate(q.from) || !isValidDate(q.to)) throw Errors.validation("Datas inválidas.");
  await rateLimit(db(), "avail-ip", clientIpHash(req), 120, 60);
  return publicAvailability(db(), q);
});
