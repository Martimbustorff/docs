import { getBookingForCalendar } from "@/lib/bookings";
import { Errors } from "@/lib/errors";
import { clientIpHash, route } from "@/lib/http";
import { buildIcs } from "@/lib/ics";

export const dynamic = "force-dynamic";
type Ctx = { params: Promise<{ token: string }> };

export const GET = route<Ctx>(async (req, { params }) => {
  const { token } = await params;
  const { entry, settings } = await getBookingForCalendar(token, { ipHash: clientIpHash(req) });
  if (entry.status === "cancelled") throw Errors.notFound("Marcação cancelada.");
  return new Response(buildIcs(entry, settings), {
    headers: {
      "Content-Type": "text/calendar; charset=utf-8",
      "Content-Disposition": 'attachment; filename="marcacao-la-barberia.ics"',
    },
  });
});
