import { NextResponse } from "next/server";
import { createAdminAppointment } from "@/lib/admin";
import { db } from "@/lib/db";
import { readJson, route } from "@/lib/http";
import { requireAdminApi } from "@/lib/session";
import { adminAppointmentInput } from "@/lib/validation";

export const dynamic = "force-dynamic";

/** Marcação criada pelo Carlos (telefone, WhatsApp, presencial). O link privado só é mostrado agora, uma vez. */
export const POST = route(async (req) => {
  await requireAdminApi(req);
  const input = await readJson(req, adminAppointmentInput);
  const { entry, link } = await createAdminAppointment(db(), input);
  return NextResponse.json({ id: entry.id, link }, { status: 201 });
});
