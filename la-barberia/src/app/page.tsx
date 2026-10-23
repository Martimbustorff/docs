import Link from "next/link";
import { db } from "@/lib/db";
import { getSettings, listActiveServices, listActiveStaff, listWorkingHours } from "@/lib/data";
import { formatEuro } from "@/lib/time";
import { instagramLink, mapsLink, prettyPhone, waLink, weeklySummary } from "@/lib/site";
import { SiteHeader, DemoBanner } from "./site-chrome";

export const dynamic = "force-dynamic";

export default async function Home() {
  const q = db();
  const [settings, services, staff] = await Promise.all([getSettings(q), listActiveServices(q), listActiveStaff(q)]);
  const hours = weeklySummary(await listWorkingHours(q, staff.map((s) => s.id)));
  const open = settings.onlineBookingOpen && services.length > 0;

  return (
    <>
      <DemoBanner settings={settings} />
      <SiteHeader settings={settings} open={open} />
      <main id="conteudo">
        <section className="hero">
          <div className="wrap">
            <p className="eyebrow">Barbearia · Cascais · Desde 2014</p>
            <h1>{settings.shopName}</h1>
            <p className="lead">{settings.tagline}</p>
            <div className="row" style={{ justifyContent: "center" }}>
              {open ? (
                <Link className="btn" href="/marcar">Marcar agora</Link>
              ) : (
                <a className="btn" href={waLink(settings)}>Marcar por WhatsApp</a>
              )}
              <a className="btn secondary" href={waLink(settings)}>Falar por WhatsApp</a>
            </div>
            <p className="muted small" style={{ marginTop: "1.25rem" }}>
              Atendimento com ou sem marcação. A marcação online garante a sua hora.
            </p>
          </div>
        </section>

        <section className="section" aria-labelledby="servicos">
          <div className="wrap narrow">
            <h2 id="servicos">Serviços</h2>
            <p className="muted">Cortes clássicos e modernos, barba e cuidados masculinos.</p>
            {services.length === 0 ? (
              <p className="muted">Em breve.</p>
            ) : (
              <ul className="services">
                {services.map((s) => (
                  <li key={s.id}>
                    <div>
                      <strong>{s.name}</strong>
                      <span className="muted small" style={{ display: "block" }}>
                        {s.durationMin} min{s.description ? ` · ${s.description}` : ""}
                      </span>
                    </div>
                    <span className="price">{formatEuro(s.priceCents)}</span>
                  </li>
                ))}
              </ul>
            )}
            {settings.demoMode && <p className="muted small">Preços e durações provisórios, a confirmar pela barbearia.</p>}
          </div>
        </section>

        <section className="section" aria-labelledby="horario">
          <div className="wrap narrow">
            <h2 id="horario">Horário</h2>
            <table className="hours">
              <tbody>
                {hours.map((h) => (
                  <tr key={h.day}>
                    <td className="cap">{h.day}</td>
                    <td>{h.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {settings.demoMode && <p className="muted small" style={{ marginTop: ".75rem" }}>Horário provisório, a confirmar pela barbearia.</p>}
          </div>
        </section>

        <section className="section" aria-labelledby="contactos">
          <div className="wrap narrow">
            <h2 id="contactos">Localização e contacto</h2>
            <p>
              {settings.address}
              <br />
              <a href={mapsLink(settings)} target="_blank" rel="noopener noreferrer">Ver no mapa</a>
            </p>
            <p>
              WhatsApp: <a href={waLink(settings)}>{prettyPhone(settings.whatsappE164)}</a>
              <br />
              Instagram: <a href={instagramLink(settings)} target="_blank" rel="noopener noreferrer">@{settings.instagram}</a>
            </p>
            <p className="muted small">
              O WhatsApp serve para falar connosco. A confirmação da marcação online aparece no ecrã e no link privado da marcação; não enviamos confirmações automáticas por WhatsApp.
            </p>
          </div>
        </section>
      </main>
      <footer className="site-footer">
        <div className="wrap narrow row between">
          <span>© {settings.shopName}</span>
          <Link href="/privacidade">Privacidade</Link>
        </div>
      </footer>
    </>
  );
}
