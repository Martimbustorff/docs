import Link from "next/link";
import type { Metadata } from "next";
import { db } from "@/lib/db";
import { getSettings, listActiveServices, listActiveStaff } from "@/lib/data";
import { addDays, localDate } from "@/lib/time";
import { waLink } from "@/lib/site";
import { DemoBanner, SiteHeader } from "../site-chrome";
import Wizard from "./Wizard";

export const dynamic = "force-dynamic";
export const metadata: Metadata = { title: "Marcar" };

export default async function MarcarPage() {
  const q = db();
  const [settings, services, staff] = await Promise.all([getSettings(q), listActiveServices(q), listActiveStaff(q)]);
  const today = localDate(new Date());
  const open = settings.onlineBookingOpen && services.length > 0 && staff.length > 0;

  return (
    <>
      <DemoBanner settings={settings} />
      <SiteHeader settings={settings} open={false} />
      <main id="conteudo" className="wrap narrow" style={{ padding: "2rem 1rem 4rem" }}>
        <h1 style={{ fontSize: "2.2rem" }}>Marcar</h1>
        {open ? (
          <Wizard
            services={services.map((s) => ({ id: s.id, name: s.name, description: s.description, priceCents: s.priceCents, durationMin: s.durationMin }))}
            staff={staff.map((s) => ({ id: s.id, name: s.name }))}
            today={today}
            maxDate={addDays(today, settings.horizonDays)}
            shop={{ whatsappLink: waLink(settings), cancelDeadlineHours: settings.cancelDeadlineHours }}
          />
        ) : (
          <div className="notice">
            <p>As marcações online estão temporariamente indisponíveis.</p>
            <a className="btn" href={waLink(settings)}>Contactar por WhatsApp</a>
          </div>
        )}
        <p className="muted small" style={{ marginTop: "2rem" }}>
          <Link href="/">← Voltar ao início</Link>
        </p>
      </main>
    </>
  );
}
