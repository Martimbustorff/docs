"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, dayParts, mondayOf, shiftDate } from "../../client-api";

interface Entry {
  id: string;
  kind: "appointment" | "block";
  staffId: string;
  staffName: string;
  startLocal: string;
  endLocal: string;
  status: "confirmed" | "completed" | "no_show" | "cancelled";
  blockKind: "block" | "vacation" | "holiday" | null;
  blockGroup: string | null;
  reason: string | null;
  serviceName: string | null;
  customerName: string | null;
  customerPhone: string | null;
  notes: string | null;
  source: "online" | "phone" | "whatsapp" | "walk_in" | null;
  rescheduleCount: number;
}
interface Staff { id: string; name: string }
interface Service { id: string; name: string; durationMin: number }

const STATUS: Record<Entry["status"], string> = { confirmed: "Confirmada", completed: "Concluída", no_show: "Falta", cancelled: "Cancelada" };
const SOURCE: Record<string, string> = { online: "Online", phone: "Telefone", whatsapp: "WhatsApp", walk_in: "Presencial" };
const BLOCK: Record<string, string> = { block: "Bloqueio", vacation: "Férias", holiday: "Feriado" };
const hm = (s: string) => s.slice(11, 16);
const dayOf = (s: string) => s.slice(0, 10);

export default function Agenda({ today, nowLocal, staff, services }: { today: string; nowLocal: string; staff: Staff[]; services: Service[] }) {
  const [view, setView] = useState<"upcoming" | "day" | "week">("upcoming");
  const [date, setDate] = useState(today);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [showCancelled, setShowCancelled] = useState(false);

  const range = useMemo(
    () =>
      view === "upcoming"
        ? { from: today, to: shiftDate(today, 61) }
        : view === "day"
          ? { from: date, to: date }
          : { from: mondayOf(date), to: shiftDate(mondayOf(date), 6) },
    [view, date, today],
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api<{ entries: Entry[] }>(`/api/admin/entries?from=${range.from}&to=${range.to}`);
      setEntries(r.entries);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro ao carregar a agenda.");
    } finally {
      setLoading(false);
    }
  }, [range.from, range.to]);

  useEffect(() => {
    load();
    // Atualiza sozinha de minuto a minuto para as marcações novas aparecerem sem recarregar a página.
    const id = setInterval(() => document.visibilityState === "visible" && load(), 60_000);
    return () => clearInterval(id);
  }, [load]);

  async function run(fn: () => Promise<unknown>, ok?: string) {
    setError(null);
    setMessage(null);
    try {
      await fn();
      if (ok) setMessage(ok);
      await load();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro.");
      return false;
    }
  }

  const forDay = (d: string) =>
    entries.filter((e) => e.startLocal < `${shiftDate(d, 1)}T00:00` && e.endLocal > `${d}T00:00` && (showCancelled || e.status !== "cancelled"));

  const dayLabel = (d: string) => dayParts(d).long;

  return (
    <div className="stack">
      <h1 style={{ fontSize: "2rem" }}>Agenda</h1>

      <div className="toolbar" role="group" aria-label="Navegação da agenda">
        <button className={`btn small ${view === "upcoming" ? "" : "secondary"}`} type="button" aria-pressed={view === "upcoming"} onClick={() => setView("upcoming")}>Próximas</button>
        <button className={`btn small ${view === "day" ? "" : "secondary"}`} type="button" aria-pressed={view === "day"} onClick={() => setView("day")}>Dia</button>
        <button className={`btn small ${view === "week" ? "" : "secondary"}`} type="button" aria-pressed={view === "week"} onClick={() => setView("week")}>Semana</button>
        {view !== "upcoming" && (
          <>
            <button className="btn secondary small" type="button" onClick={() => setDate(shiftDate(date, view === "day" ? -1 : -7))}>← Anterior</button>
            <button className="btn secondary small" type="button" onClick={() => setDate(today)}>Hoje</button>
            <button className="btn secondary small" type="button" onClick={() => setDate(shiftDate(date, view === "day" ? 1 : 7))}>Seguinte →</button>
            <label className="sr-only" htmlFor="agenda-date">Ir para a data</label>
            <input id="agenda-date" type="date" value={date} onChange={(e) => e.target.value && setDate(e.target.value)} />
          </>
        )}
        <label className="check" style={{ marginLeft: ".5rem" }}>
          <input type="checkbox" checked={showCancelled} onChange={(e) => setShowCancelled(e.target.checked)} /> Mostrar canceladas
        </label>
      </div>

      <div aria-live="polite">
        {error && <div className="error" role="alert">{error}</div>}
        {message && <div className="ok" role="status">{message}</div>}
      </div>

      <h2 className="cap">
        {view === "upcoming" ? "Próximas marcações" : view === "day" ? dayLabel(date) : `${dayLabel(range.from)} – ${dayLabel(range.to)}`}
      </h2>
      {loading && <p role="status">A carregar…</p>}

      {view === "upcoming" ? (
        <Upcoming entries={entries.filter((e) => e.kind === "appointment" && e.status === "confirmed" && e.endLocal > nowLocal)} today={today} staff={staff} onRun={run} />
      ) : view === "day" ? (
        <DayList entries={forDay(date)} staff={staff} onRun={run} />
      ) : (
        <div className="week">
          {Array.from({ length: 7 }, (_, i) => shiftDate(range.from, i)).map((d) => (
            <div className="card" key={d}>
              <h3 className="cap">
                <button className="btn secondary small" type="button" onClick={() => { setDate(d); setView("day"); }}>{dayParts(d).weekday} {dayParts(d).day}</button>
              </h3>
              {forDay(d).length === 0 && <p className="muted small">Sem marcações.</p>}
              {forDay(d).map((e) => (
                <div className="mini" key={e.id}>
                  <strong>{hm(e.startLocal)}</strong>{" "}
                  {e.kind === "block" ? `${BLOCK[e.blockKind ?? "block"]}${e.reason ? `: ${e.reason}` : ""}` : `${e.customerName} · ${e.serviceName}`}
                  {e.status !== "confirmed" && <span className="muted"> ({STATUS[e.status]})</span>}
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      <div className="grid2" style={{ alignItems: "start" }}>
        <NewAppointment staff={staff} services={services} date={date} onDone={(msg) => run(async () => {}, msg)} />
        <NewBlock staff={staff} date={date} onDone={(msg) => run(async () => {}, msg)} />
      </div>
    </div>
  );
}

function Upcoming({ entries, today, staff, onRun }: { entries: Entry[]; today: string; staff: Staff[]; onRun: (fn: () => Promise<unknown>, ok?: string) => Promise<boolean> }) {
  if (entries.length === 0) return <p className="card muted">Sem marcações futuras. Quando alguém marcar, aparece aqui (a lista atualiza sozinha).</p>;
  const days = [...new Set(entries.map((e) => e.startLocal.slice(0, 10)))];
  const title = (d: string) => (d === today ? "Hoje" : d === shiftDate(today, 1) ? "Amanhã" : dayParts(d).long);
  return (
    <div className="stack">
      <p className="muted small">{entries.length} marcação(ões) confirmada(s) nos próximos 60 dias, por ordem cronológica.</p>
      {days.map((d) => (
        <section key={d} aria-label={title(d)}>
          <h3 className="cap">{title(d)}{title(d) === "Hoje" || title(d) === "Amanhã" ? <span className="muted"> · {dayParts(d).long}</span> : null}</h3>
          {entries.filter((e) => e.startLocal.startsWith(d)).map((e) => <AppointmentCard key={e.id} e={e} staff={staff} onRun={onRun} />)}
        </section>
      ))}
    </div>
  );
}

function DayList({ entries, staff, onRun }: { entries: Entry[]; staff: Staff[]; onRun: (fn: () => Promise<unknown>, ok?: string) => Promise<boolean> }) {
  if (entries.length === 0) return <p className="card muted">Sem marcações nem bloqueios neste dia.</p>;
  const groups = staff.length > 1 ? staff.map((s) => ({ s, items: entries.filter((e) => e.staffId === s.id) })) : [{ s: staff[0], items: entries }];
  return (
    <div className="stack">
      {groups.map(({ s, items }) => (
        <section key={s?.id ?? "x"} aria-label={s?.name}>
          {staff.length > 1 && <h3>{s.name}</h3>}
          {items.length === 0 && <p className="muted">Sem marcações.</p>}
          {items.map((e) => (e.kind === "block" ? <BlockCard key={e.id} e={e} onRun={onRun} /> : <AppointmentCard key={e.id} e={e} staff={staff} onRun={onRun} />))}
        </section>
      ))}
    </div>
  );
}

function BlockCard({ e, onRun }: { e: Entry; onRun: (fn: () => Promise<unknown>, ok?: string) => Promise<boolean> }) {
  return (
    <div className="card entry block">
      <div className="row between">
        <div>
          <span className="time-range">{hm(e.startLocal)}–{hm(e.endLocal)}</span>{" "}
          <strong>{BLOCK[e.blockKind ?? "block"]}</strong>{e.reason ? `: ${e.reason}` : ""}
          <span className="muted small"> · {e.staffName} · {dayOf(e.startLocal)} a {dayOf(e.endLocal)}</span>
        </div>
        <button className="btn danger small" type="button" onClick={() => e.blockGroup && confirm("Remover este bloqueio?") && onRun(() => api(`/api/admin/blocks/${e.blockGroup}`, { method: "DELETE" }), "Bloqueio removido.")}>Remover</button>
      </div>
    </div>
  );
}

function AppointmentCard({ e, staff, onRun }: { e: Entry; staff: Staff[]; onRun: (fn: () => Promise<unknown>, ok?: string) => Promise<boolean> }) {
  const [moving, setMoving] = useState(false);
  const [d, setD] = useState(dayOf(e.startLocal));
  const [t, setT] = useState(hm(e.startLocal));
  const [staffId, setStaffId] = useState(e.staffId);
  const [notes, setNotes] = useState(e.notes ?? "");
  const [editNotes, setEditNotes] = useState(false);
  const patch = (body: object, ok: string) => onRun(() => api(`/api/admin/appointments/${e.id}`, { method: "PATCH", body }), ok);
  const wa = e.customerPhone ? `https://wa.me/${e.customerPhone.replace(/\D/g, "")}` : "";

  return (
    <div className={`card entry ${e.status === "cancelled" ? "cancelled" : ""}`}>
      <div className="row between">
        <div>
          <span className="time-range">{hm(e.startLocal)}–{hm(e.endLocal)}</span>{" "}
          <span className={`status ${e.status}`}>{STATUS[e.status]}</span>
        </div>
        <span className="muted small">{SOURCE[e.source ?? ""] ?? e.source}{e.rescheduleCount ? ` · remarcada ${e.rescheduleCount}×` : ""}</span>
      </div>
      <div>
        <strong>{e.customerName}</strong> · {e.serviceName}
        <br />
        {e.customerPhone && (
          <span className="small">
            <a href={`tel:${e.customerPhone}`}>{e.customerPhone}</a> · <a href={wa} target="_blank" rel="noopener noreferrer">WhatsApp</a>
          </span>
        )}
        {e.notes && !editNotes && <p className="small" style={{ margin: ".4rem 0 0" }}>Nota: {e.notes}</p>}
      </div>

      <div className="row">
        {e.status === "confirmed" && (
          <>
            <button className="btn small" type="button" onClick={() => patch({ status: "completed" }, "Marcada como concluída.")}>Concluída</button>
            <button className="btn secondary small" type="button" onClick={() => patch({ status: "no_show" }, "Falta registada.")}>Falta</button>
            <button className="btn secondary small" type="button" onClick={() => setMoving((m) => !m)}>Remarcar</button>
            <button className="btn danger small" type="button" onClick={() => confirm(`Cancelar a marcação de ${e.customerName}?`) && patch({ status: "cancelled" }, "Marcação cancelada.")}>Cancelar</button>
          </>
        )}
        {e.status !== "confirmed" && (
          <button className="btn secondary small" type="button" onClick={() => patch({ status: "confirmed" }, "Marcação reposta como confirmada.")}>Repor como confirmada</button>
        )}
        <button className="btn secondary small" type="button" onClick={() => setEditNotes((v) => !v)}>Nota</button>
      </div>

      {moving && (
        <form
          className="row"
          onSubmit={async (ev) => {
            ev.preventDefault();
            const ok = await patch({ start: `${d}T${t}`, ...(staffId !== e.staffId ? { staffId } : {}) }, "Marcação remarcada.");
            if (ok) setMoving(false);
          }}
        >
          <div><label htmlFor={`d-${e.id}`}>Dia</label><input id={`d-${e.id}`} type="date" value={d} onChange={(x) => setD(x.target.value)} required /></div>
          <div><label htmlFor={`t-${e.id}`}>Hora</label><input id={`t-${e.id}`} type="time" step={300} value={t} onChange={(x) => setT(x.target.value)} required /></div>
          {staff.length > 1 && (
            <div><label htmlFor={`s-${e.id}`}>Profissional</label>
              <select id={`s-${e.id}`} value={staffId} onChange={(x) => setStaffId(x.target.value)}>{staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select>
            </div>
          )}
          <button className="btn small" type="submit">Guardar nova hora</button>
        </form>
      )}
      {editNotes && (
        <form className="row" onSubmit={async (ev) => { ev.preventDefault(); if (await patch({ notes }, "Nota guardada.")) setEditNotes(false); }}>
          <div className="grow"><label htmlFor={`n-${e.id}`}>Nota interna</label><input id={`n-${e.id}`} value={notes} maxLength={500} onChange={(x) => setNotes(x.target.value)} /></div>
          <button className="btn small" type="submit">Guardar</button>
        </form>
      )}
    </div>
  );
}

function NewAppointment({ staff, services, date, onDone }: { staff: Staff[]; services: Service[]; date: string; onDone: (msg: string) => void }) {
  const [serviceId, setServiceId] = useState(services[0]?.id ?? "");
  const [staffId, setStaffId] = useState(staff[0]?.id ?? "");
  const [d, setD] = useState(date);
  const [t, setT] = useState("10:00");
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [notes, setNotes] = useState("");
  const [source, setSource] = useState("phone");
  const [error, setError] = useState<string | null>(null);
  const [link, setLink] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setD(date), [date]);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    setLink(null);
    try {
      const r = await api<{ link: string }>("/api/admin/appointments", {
        body: { serviceId, staffId, start: `${d}T${t}`, name, phone, notes: notes || undefined, source },
      });
      setLink(r.link);
      setName(""); setPhone(""); setNotes("");
      onDone("Marcação criada.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="card" open>
      <summary><strong>Nova marcação</strong> (telefone, WhatsApp, presencial)</summary>
      <form className="stack" onSubmit={submit} style={{ marginTop: "1rem" }}>
        {error && <div className="error" role="alert">{error}</div>}
        <div className="grid2">
          <div className="field"><label htmlFor="na-service">Serviço</label>
            <select id="na-service" value={serviceId} onChange={(e) => setServiceId(e.target.value)}>{services.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.durationMin} min)</option>)}</select></div>
          {staff.length > 1 && (
            <div className="field"><label htmlFor="na-staff">Profissional</label>
              <select id="na-staff" value={staffId} onChange={(e) => setStaffId(e.target.value)}>{staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></div>
          )}
          <div className="field"><label htmlFor="na-date">Dia</label><input id="na-date" type="date" value={d} onChange={(e) => setD(e.target.value)} required /></div>
          <div className="field"><label htmlFor="na-time">Hora</label><input id="na-time" type="time" step={300} value={t} onChange={(e) => setT(e.target.value)} required /></div>
          <div className="field"><label htmlFor="na-name">Nome</label><input id="na-name" value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} /></div>
          <div className="field"><label htmlFor="na-phone">Telemóvel</label><input id="na-phone" type="tel" value={phone} onChange={(e) => setPhone(e.target.value)} required /></div>
          <div className="field"><label htmlFor="na-source">Origem</label>
            <select id="na-source" value={source} onChange={(e) => setSource(e.target.value)}><option value="phone">Telefone</option><option value="whatsapp">WhatsApp</option><option value="walk_in">Presencial</option></select></div>
        </div>
        <div className="field"><label htmlFor="na-notes">Nota (opcional)</label><input id="na-notes" value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={500} /></div>
        <p className="muted small">Pode marcar fora do horário habitual; só não pode sobrepor outra marcação ou bloqueio.</p>
        <button className="btn" type="submit" disabled={busy || !serviceId || !staffId}>{busy ? "A guardar…" : "Criar marcação"}</button>
        {link && (
          <div className="ok">
            <p>Link privado do cliente (só é mostrado agora; pode enviá-lo por WhatsApp):</p>
            <input readOnly value={link} onFocus={(e) => e.currentTarget.select()} aria-label="Link privado da marcação" />
            <button className="btn secondary small" type="button" style={{ marginTop: ".5rem" }} onClick={() => navigator.clipboard?.writeText(link)}>Copiar link</button>
          </div>
        )}
      </form>
    </details>
  );
}

function NewBlock({ staff, date, onDone }: { staff: Staff[]; date: string; onDone: (msg: string) => void }) {
  const [staffId, setStaffId] = useState("all");
  const [kind, setKind] = useState("block");
  const [from, setFrom] = useState(date);
  const [to, setTo] = useState(date);
  const [allDay, setAllDay] = useState(true);
  const [t1, setT1] = useState("13:00");
  const [t2, setT2] = useState("14:00");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setFrom(date); setTo(date); }, [date]);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const start = allDay ? `${from}T00:00` : `${from}T${t1}`;
      const end = allDay ? `${shiftDate(to, 1)}T00:00` : `${to}T${t2}`;
      await api("/api/admin/blocks", { body: { staffId, kind, start, end, reason: reason || undefined } });
      setReason("");
      onDone("Bloqueio criado.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Erro.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="card">
      <summary><strong>Bloquear horário</strong> (férias, feriados, pausas)</summary>
      <form className="stack" onSubmit={submit} style={{ marginTop: "1rem" }}>
        {error && <div className="error" role="alert">{error}</div>}
        <div className="grid2">
          <div className="field"><label htmlFor="nb-kind">Tipo</label>
            <select id="nb-kind" value={kind} onChange={(e) => setKind(e.target.value)}><option value="block">Bloqueio</option><option value="vacation">Férias</option><option value="holiday">Feriado</option></select></div>
          {staff.length > 1 && (
            <div className="field"><label htmlFor="nb-staff">Para</label>
              <select id="nb-staff" value={staffId} onChange={(e) => setStaffId(e.target.value)}><option value="all">Toda a barbearia</option>{staff.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></div>
          )}
          <div className="field"><label htmlFor="nb-from">De (dia)</label><input id="nb-from" type="date" value={from} onChange={(e) => setFrom(e.target.value)} required /></div>
          <div className="field"><label htmlFor="nb-to">Até (dia)</label><input id="nb-to" type="date" value={to} min={from} onChange={(e) => setTo(e.target.value)} required /></div>
        </div>
        <label className="check"><input type="checkbox" checked={allDay} onChange={(e) => setAllDay(e.target.checked)} /> Dia(s) inteiro(s)</label>
        {!allDay && (
          <div className="grid2">
            <div className="field"><label htmlFor="nb-t1">Das</label><input id="nb-t1" type="time" step={300} value={t1} onChange={(e) => setT1(e.target.value)} /></div>
            <div className="field"><label htmlFor="nb-t2">Às</label><input id="nb-t2" type="time" step={300} value={t2} onChange={(e) => setT2(e.target.value)} /></div>
          </div>
        )}
        <div className="field"><label htmlFor="nb-reason">Motivo (opcional)</label><input id="nb-reason" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={120} /></div>
        <p className="muted small">Não é possível bloquear um período que já tenha marcações: cancele ou remarque-as primeiro.</p>
        <button className="btn" type="submit" disabled={busy}>{busy ? "A guardar…" : "Criar bloqueio"}</button>
      </form>
    </details>
  );
}
