"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ApiClientError, api, dayParts, longLisbon, shiftDate } from "../../client-api";

interface View {
  status: "confirmed" | "completed" | "no_show" | "cancelled";
  serviceName: string;
  durationMin: number;
  staffName: string;
  startsAt: string;
  endsAt: string;
  startLabel: string;
  customerName: string;
  canModify: boolean;
  modifyUntil: string;
  rescheduleCount: number;
  maxReschedules: number;
  shop: { name: string; address: string; whatsappE164: string; cancelDeadlineHours: number };
}
interface Slot { startsAt: string; label: string }

const STATUS: Record<View["status"], string> = {
  confirmed: "Confirmada",
  completed: "Concluída",
  no_show: "Falta registada",
  cancelled: "Cancelada",
};

const calStamp = (iso: string) => new Date(iso).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");

export default function Manage(props: { token: string; isNew: boolean; today: string; maxDate: string; whatsappLink: string; instagramLink: string }) {
  const { token, today, maxDate, whatsappLink } = props;
  const base = `/api/public/bookings/${token}`;
  const [view, setView] = useState<View | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(props.isNew ? "A sua marcação está confirmada." : null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [busy, setBusy] = useState(false);
  const [resched, setResched] = useState(false);
  const [days, setDays] = useState<Record<string, Slot[]> | null>(null);
  const [date, setDate] = useState("");
  const [slot, setSlot] = useState<Slot | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ booking: View }>(base);
      setView(r.booking);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Não foi possível abrir a marcação.");
    }
  }, [base]);
  useEffect(() => {
    load();
  }, [load]);

  async function act<T>(fn: () => Promise<T>, ok: string) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setMessage(ok);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ocorreu um erro.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    const done = await act(async () => setView((await api<{ booking: View }>(`${base}/cancel`, { body: {} })).booking), "A marcação foi cancelada. A vaga ficou livre.");
    if (done) setConfirmCancel(false);
  }

  async function openResched() {
    setResched(true);
    setSlot(null);
    setError(null);
    try {
      const merged: Record<string, Slot[]> = {};
      let from = today;
      while (from <= maxDate) {
        const to = shiftDate(from, 61) < maxDate ? shiftDate(from, 61) : maxDate;
        const r = await api<{ days: Record<string, Slot[]> }>(`${base}/availability?from=${from}&to=${to}`);
        Object.assign(merged, r.days);
        from = shiftDate(to, 1);
      }
      setDays(merged);
      setDate(Object.keys(merged).sort()[0] ?? "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Não foi possível carregar as vagas.");
    }
  }

  async function reschedule() {
    if (!slot) return;
    try {
      setBusy(true);
      setError(null);
      const r = await api<{ booking: View }>(`${base}/reschedule`, { body: { startsAt: slot.startsAt } });
      setView(r.booking);
      setMessage("Marcação remarcada. A sua hora anterior ficou livre.");
      setResched(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Ocorreu um erro.");
      if (e instanceof ApiClientError && e.code === "SLOT_TAKEN") {
        setSlot(null);
        openResched(); // atualiza as vagas; a marcação original mantém-se
      }
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <div className="stack">
        <h1 style={{ fontSize: "2rem" }}>Marcação não encontrada</h1>
        <p className="error" role="alert">{loadError}</p>
        <p className="muted">Confirme que abriu o link completo. Se precisar de ajuda, fale connosco.</p>
        <a className="btn" href={whatsappLink}>Contactar por WhatsApp</a>
      </div>
    );
  }
  if (!view) return <p role="status">A carregar…</p>;

  const gcal = new URLSearchParams({
    action: "TEMPLATE",
    text: `${view.shop.name} – ${view.serviceName}`,
    dates: `${calStamp(view.startsAt)}/${calStamp(view.endsAt)}`,
    location: view.shop.address,
  }).toString();
  const dayList = days ? Object.keys(days).sort() : [];
  const until = longLisbon(view.modifyUntil);

  return (
    <div className="stack">
      <h1 style={{ fontSize: "2.2rem" }}>A sua marcação</h1>
      <div aria-live="polite">
        {message && <div className="ok" role="status">{message}</div>}
        {error && <div className="error" role="alert">{error}</div>}
      </div>

      <dl className="summary card">
        <dt>Estado</dt><dd><span className={`status ${view.status}`}>{STATUS[view.status]}</span></dd>
        <dt>Nome</dt><dd>{view.customerName}</dd>
        <dt>Serviço</dt><dd>{view.serviceName} · {view.durationMin} min</dd>
        <dt>Profissional</dt><dd>{view.staffName}</dd>
        <dt>Quando</dt><dd className="cap">{view.startLabel}</dd>
        <dt>Onde</dt><dd>{view.shop.name}, {view.shop.address}</dd>
      </dl>

      {view.status === "confirmed" && (
        <>
          <div className="row">
            <a className="btn secondary" href={`${base}/ics`}>Adicionar ao calendário (.ics)</a>
            <a className="btn secondary" href={`https://calendar.google.com/calendar/render?${gcal}`} target="_blank" rel="noopener noreferrer">Google Calendar</a>
          </div>
          <p className="muted small">
            Guarde este link: é a forma de ver, remarcar ou cancelar esta marcação. Quem tiver o link consegue geri-la, por isso não o partilhe.
          </p>
        </>
      )}

      {view.status === "confirmed" && view.canModify && !resched && (
        <div className="stack">
          <div className="row">
            {view.rescheduleCount < view.maxReschedules && (
              <button className="btn" type="button" onClick={openResched} disabled={busy}>Remarcar</button>
            )}
            {!confirmCancel && <button className="btn danger" type="button" onClick={() => setConfirmCancel(true)} disabled={busy}>Cancelar marcação</button>}
          </div>
          <p className="muted small">Pode alterar ou cancelar online até {until}.</p>
          {confirmCancel && (
            <div className="notice stack" role="alertdialog" aria-label="Confirmar cancelamento">
              <p><strong>Tem a certeza de que quer cancelar?</strong></p>
              <div className="row">
                <button className="btn danger" type="button" onClick={cancel} disabled={busy}>Sim, cancelar</button>
                <button className="btn secondary" type="button" onClick={() => setConfirmCancel(false)} disabled={busy}>Não, manter</button>
              </div>
            </div>
          )}
        </div>
      )}

      {view.status === "confirmed" && !view.canModify && (
        <div className="notice">
          <p>O prazo para alterar ou cancelar online já terminou ({view.shop.cancelDeadlineHours} h antes). Para qualquer alteração, fale connosco.</p>
          <a className="btn" href={whatsappLink}>Contactar por WhatsApp</a>
          <p className="muted small" style={{ marginTop: ".75rem" }}>O WhatsApp é um contacto manual com a barbearia, não uma confirmação automática.</p>
        </div>
      )}

      {resched && (
        <section className="stack" aria-labelledby="h-resched">
          <h2 id="h-resched">Escolha a nova hora</h2>
          <p className="muted small">Se a nova hora falhar, a sua marcação atual mantém-se.</p>
          {!days && !error && <p role="status">A carregar vagas…</p>}
          {days && dayList.length === 0 && <p className="notice">Não há vagas disponíveis. Contacte-nos por WhatsApp.</p>}
          {dayList.length > 0 && (
            <>
              <div className="days" role="group" aria-label="Dias com vagas">
                {dayList.map((d) => {
                  const p = dayParts(d);
                  return (
                    <button key={d} type="button" className="day" aria-pressed={date === d} aria-label={p.long} onClick={() => { setDate(d); setSlot(null); }}>
                      <span className="wd">{p.weekday}</span><span className="dn">{p.day}</span><span className="mn">{p.month}</span>
                    </button>
                  );
                })}
              </div>
              {date && days?.[date] && (
                <div className="times" role="group" aria-label={`Horas em ${dayParts(date).long}`}>
                  {days[date].map((s) => (
                    <button key={s.startsAt} type="button" className="time" aria-pressed={slot?.startsAt === s.startsAt} onClick={() => setSlot(s)}>{s.label}</button>
                  ))}
                </div>
              )}
            </>
          )}
          <div className="row">
            <button className="btn secondary" type="button" onClick={() => setResched(false)} disabled={busy}>Voltar</button>
            <button className="btn" type="button" disabled={!slot || busy} onClick={reschedule}>Confirmar nova hora</button>
          </div>
        </section>
      )}

      {view.status === "cancelled" && (
        <div className="row">
          <Link className="btn" href="/marcar">Fazer nova marcação</Link>
        </div>
      )}
    </div>
  );
}
