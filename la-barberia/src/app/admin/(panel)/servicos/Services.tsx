"use client";

import { useState } from "react";
import { api } from "../../../client-api";

interface Svc { id: string; name: string; description: string; durationMin: number; active: boolean; sortOrder: number; isDemo: boolean }

export default function Services({ initial }: { initial: Svc[] }) {
  const [items, setItems] = useState<Svc[]>(initial);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function reload() {
    setItems((await api<{ services: Svc[] }>("/api/admin/services")).services);
  }

  return (
    <div className="stack">
      <h1 style={{ fontSize: "2rem" }}>Serviços</h1>
      <p className="muted">Estes serviços e durações aparecem no site e determinam as vagas (por omissão há só um: &quot;Corte de cabelo&quot;, 30 minutos). Ao guardar um serviço de demonstração, ele passa a ser um serviço real.</p>
      <div aria-live="polite">
        {error && <div className="error" role="alert">{error}</div>}
        {message && <div className="ok" role="status">{message}</div>}
      </div>
      {items.map((s) => (
        <Row key={s.id} s={s} onError={setError} onMessage={setMessage} onChanged={reload} />
      ))}
      <Row s={{ id: "", name: "", description: "", durationMin: 30, active: true, sortOrder: items.length + 1, isDemo: false }} isNew onError={setError} onMessage={setMessage} onChanged={reload} />
    </div>
  );
}

function Row({ s, isNew, onError, onMessage, onChanged }: { s: Svc; isNew?: boolean; onError: (m: string | null) => void; onMessage: (m: string | null) => void; onChanged: () => Promise<void> }) {
  const [name, setName] = useState(s.name);
  const [description, setDescription] = useState(s.description);
  const [duration, setDuration] = useState(String(s.durationMin));
  const [active, setActive] = useState(s.active);
  const [order, setOrder] = useState(String(s.sortOrder));
  const [busy, setBusy] = useState(false);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    onError(null);
    onMessage(null);
    setBusy(true);
    try {
      const body = { name, description, durationMin: Number(duration), active, sortOrder: Number(order) };
      if (isNew) await api("/api/admin/services", { body });
      else await api(`/api/admin/services/${s.id}`, { method: "PUT", body });
      onMessage(isNew ? "Serviço criado." : "Serviço guardado.");
      if (isNew) { setName(""); setDescription(""); }
      await onChanged();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Erro.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!confirm(`Remover o serviço "${s.name}"? Se já tiver marcações, fica apenas desativado.`)) return;
    onError(null);
    try {
      const r = await api<{ deleted: boolean; deactivated: boolean }>(`/api/admin/services/${s.id}`, { method: "DELETE" });
      onMessage(r.deleted ? "Serviço removido." : "O serviço tem histórico de marcações: foi desativado.");
      await onChanged();
    } catch (err) {
      onError(err instanceof Error ? err.message : "Erro.");
    }
  }

  return (
    <form className="card stack" onSubmit={save} aria-label={isNew ? "Novo serviço" : `Serviço ${s.name}`}>
      <div className="row between">
        <h2 style={{ fontSize: "1.2rem", margin: 0 }}>{isNew ? "Novo serviço" : s.name}</h2>
        {s.isDemo && <span className="status">Demonstração</span>}
      </div>
      <div className="grid2">
        <div className="field"><label htmlFor={`n-${s.id}`}>Nome</label><input id={`n-${s.id}`} value={name} onChange={(e) => setName(e.target.value)} required maxLength={80} /></div>
        <div className="field"><label htmlFor={`d-${s.id}`}>Duração (minutos)</label><input id={`d-${s.id}`} type="number" min={5} max={480} step={5} value={duration} onChange={(e) => setDuration(e.target.value)} required /></div>
        <div className="field"><label htmlFor={`o-${s.id}`}>Ordem</label><input id={`o-${s.id}`} type="number" min={0} max={1000} value={order} onChange={(e) => setOrder(e.target.value)} /></div>
      </div>
      <div className="field"><label htmlFor={`desc-${s.id}`}>Descrição (opcional)</label><input id={`desc-${s.id}`} value={description} onChange={(e) => setDescription(e.target.value)} maxLength={300} /></div>
      <label className="check"><input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} /> Visível no site e disponível para marcação</label>
      <div className="row">
        <button className="btn" type="submit" disabled={busy}>{isNew ? "Adicionar" : "Guardar"}</button>
        {!isNew && <button className="btn danger" type="button" onClick={remove}>Remover</button>}
      </div>
    </form>
  );
}
