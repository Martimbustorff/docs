import { NextResponse } from "next/server";
import { db } from "@/lib/db";

export const dynamic = "force-dynamic";

/** Verificação de saúde (sem dados pessoais). Também conta como atividade para planos gratuitos que pausam por inatividade. */
export async function GET() {
  try {
    await db()`select 1`;
    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ ok: false }, { status: 503 });
  }
}
