import type { Metadata } from "next";
import { db } from "@/lib/db";
import { getSettings } from "@/lib/data";
import { addDays, localDate } from "@/lib/time";
import { instagramLink, waLink } from "@/lib/site";
import { SiteHeader, DemoBanner } from "../../site-chrome";
import Manage from "./Manage";

export const dynamic = "force-dynamic";
export const metadata: Metadata = {
  title: "A sua marcação",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default async function MarcacaoPage({ params, searchParams }: { params: Promise<{ token: string }>; searchParams: Promise<{ novo?: string }> }) {
  const { token } = await params;
  const { novo } = await searchParams;
  const settings = await getSettings(db());
  const today = localDate(new Date());
  return (
    <>
      <DemoBanner settings={settings} />
      <SiteHeader settings={settings} open={false} />
      <main id="conteudo" className="wrap narrow" style={{ padding: "2rem 1rem 4rem" }}>
        <Manage
          token={token}
          isNew={novo === "1"}
          today={today}
          maxDate={addDays(today, settings.horizonDays)}
          whatsappLink={waLink(settings)}
          instagramLink={instagramLink(settings)}
        />
      </main>
    </>
  );
}
