"use client";

import { useState } from "react";
import { api } from "../../../client-api";

interface Interval { weekday: number; start: string; end: string }
interface Member { id: string; name: string; active: boolean; sortOrder: number; intervals: Interval[] }

const DAYS = ["", "Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"];

export default function Team({ initial }: { initial: Member[] }) {
  const [members, setMembers] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [newName, setNewName] = useState("");

  async function addStaff(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const r = await api<{ staff: { id: string; name: string; active: boolean; sortOrder: number } }>("/api/admin/staff", { body: { name: newName, active: true, sortOrder: members.length } });
      setMembers([...members, { ...r.staff, intervals: [] }]);
      setNewName("");
      setMessage("Profissional adicionado. Defina agora o horário semanal.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro.");
    }
  }

  return (
    <div className="stack">
      <h1 style={{ fontSize: "2rem" }}>Equipa e horários</h1>
      <p className="muted">
        O horário semanal define quando se pode marcar. As <strong>pausas</strong> são os intervalos entre dois períodos do mesmo dia (por exemplo 09:30–13:00 e 14:30–19:00). Alterar o horário não mexe nas marcações já existentes. Para férias e feriados, use <strong>Bloquear horário</strong> na Agenda.
      </p>
      <div aria-live="polite">
        {error && <div className="error" role="alert">{error}</div>}
        {message && <div className="ok" role="status">{message}</div>}
      </div>
      {members.map((m) => (
        <Member key={m.id} m={m} onError={setError} onMessage={setMessage} />
      ))}
      <form className="card row" onSubmit={addStaff}>
        <div className="grow"><label htmlFor="new-staff">Novo profissional</label><input id="new-staff" value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={60} required /></div>
        <button className="btn" type="submit">Adicionar</button>
      </form>
    </div>
  );
}

function Member({ m, onError, onMessage }: { m: Member; onError: (s: string | null) => void; onMessage: (s: string | null) => void }) {
  const [name, setName] = useState(m.name);
  const [active, setActive] = useState(m.active);
  const [intervals, setIntervals] = useState<Interval[]>(m.intervals);

  const update = (i: number, patch: Partial<Interval>) => setIntervals(intervals.map((x, k) => (k === i ? { ...x, ...patch } : x)));

  async function saveProfile(e: React.FormEvent) {
    e.preventDefault();
    onError(null); onMessage(null);
    try {
      await api(`/api/admin/staff/${m.id}`, { method: "PUT", body: { name, active, sortOrder: m.sortOrder } });
      onMessage("Profissional guardado.");
    } catch (err) {
      onError(err instanceof Error ? err.message : "Erro.");
    }
  }

  async function saveHours() {
    onError(null); onMessage(null);
    try {
      await api(`/api/admin/staff/${m.id}/hours`, { method: "PUT", body: { intervals } });
      onMessage(`Horário de ${name} guardado.`);
    } catch (err) {
      onError(err instanceof Error ? err.message : "Erro.");
    }
  }

  return (
    <section className="card stack" aria-label={m.name}>
      <form className="row" onSubmit={saveProfile}>
        <div className="grow"><label htmlFor={`sn-${m.id}`}>Nome</label><input id={`sn-${m.id}`} value={name} onChange={(e) => setName(e.target.value)} required maxLength={60} /></div>
        <label className="check"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Ativo</label>
        <button className="btn secondary" type="submit">Guardar nome</button>
      </form>

      <h2 style={{ fontSize: "1.2rem" }}>Horário semanal</h2>
      <div className="stack">
        {DAYS.slice(1).map((label, idx) => {
          const wd = idx + 1;
          const mine = intervals.map((x, i) => ({ x, i })).filter(({ x }) => x.weekday === wd);
          return (
            <div key={wd} className="row" style={{ alignItems: "flex-start" }}>
              <div style={{ width: "9rem", paddingTop: ".6rem" }}><strong>{label}</strong></div>
              <div className="intervals grow">
                {mine.length === 0 && <span className="muted" style={{ paddingTop: ".6rem" }}>Fechado</span>}
                {mine.map(({ x, i }) => (
                  <div className="interval" key={i}>
                    <label className="sr-only" htmlFor={`a-${m.id}-${i}`}>Início ({label})</label>
                    <input id={`a-${m.id}-${i}`} type="time" step={300} value={x.start} onChange={(e) => update(i, { start: e.target.value })} />
                    <span aria-hidden="true">–</span>
                    <label className="sr-only" htmlFor={`b-${m.id}-${i}`}>Fim ({label})</label>
                    <input id={`b-${m.id}-${i}`} type="time" step={300} value={x.end} onChange={(e) => update(i, { end: e.target.value })} />
                    <button className="btn secondary small" type="button" onClick={() => setIntervals(intervals.filter((_, k) => k !== i))} aria-label={`Remover período de ${label}`}>Remover</button>
                  </div>
                ))}
                <div><button className="btn secondary small" type="button" onClick={() => setIntervals([...intervals, { weekday: wd, start: mine.length ? mine[mine.length - 1].x.end : "09:00", end: mine.length ? "19:00" : "13:00" }])}>+ Adicionar período</button></div>
              </div>
            </div>
          );
        })}
      </div>
      <div><button className="btn" type="button" onClick={saveHours}>Guardar horário</button></div>
    </section>
  );
}
