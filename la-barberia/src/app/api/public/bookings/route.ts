import { db } from "@/lib/db";
import { createOnlineBooking, getBookingView } from "@/lib/bookings";
import { getSettings } from "@/lib/data";
import { sendBookingEmail } from "@/lib/email";
import { clientIpHash, readJson, route } from "@/lib/http";
import { publicBookingInput } from "@/lib/validation";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export const POST = route(async (req) => {
  const input = await readJson(req, publicBookingInput);
  const ipHash = clientIpHash(req);
  const created = await createOnlineBooking(input, { ipHash });
  if (!created.replay && created.entry.customerEmail) {
    // O email é opcional e nunca pode afetar a marcação já guardada.
    await sendBookingEmail(created.entry, created.token, await getSettings(db()));
  }
  const booking = await getBookingView(created.token, { ipHash });
  return NextResponse.json({ token: created.token, booking }, { status: created.replay ? 200 : 201 });
});
