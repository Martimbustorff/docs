"use client";

import { useState } from "react";
import { api, shiftDate } from "../../../client-api";

export default function Account({ today }: { today: string }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [from, setFrom] = useState(shiftDate(today, -30));
  const [to, setTo] = useState(shiftDate(today, 30));

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    setError(null); setMessage(null);
    try {
      await api("/api/admin/password", { body: { current, next } });
      setCurrent(""); setNext("");
      setMessage("Palavra-passe alterada. As outras sessões foram terminadas.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro.");
    }
  }

  return (
    <div className="stack">
      <h1 style={{ fontSize: "2rem" }}>Conta e exportação</h1>
      <div aria-live="polite">
        {error && <div className="error" role="alert">{error}</div>}
        {message && <div className="ok" role="status">{message}</div>}
      </div>

      <section className="card stack" aria-labelledby="exp">
        <h2 id="exp" style={{ fontSize: "1.2rem" }}>Exportar marcações (CSV)</h2>
        <p className="muted small">Abre no Excel. Contém nomes e telemóveis: guarde o ficheiro em local seguro e apague-o quando já não for necessário.</p>
        <div className="row">
          <div><label htmlFor="ef">De</label><input id="ef" type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></div>
          <div><label htmlFor="et">Até</label><input id="et" type="date" value={to} onChange={(e) => setTo(e.target.value)} /></div>
          <a className="btn" href={`/api/admin/export?from=${from}&to=${to}`}>Descarregar CSV</a>
        </div>
      </section>

      <form className="card stack" onSubmit={changePassword}>
        <h2 style={{ fontSize: "1.2rem" }}>Alterar palavra-passe</h2>
        <div className="field"><label htmlFor="cur">Palavra-passe atual</label><input id="cur" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></div>
        <div className="field"><label htmlFor="new">Nova palavra-passe <span className="hint">Mínimo de 12 caracteres.</span></label><input id="new" type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required /></div>
        <div><button className="btn" type="submit">Alterar</button></div>
      </form>
    </div>
  );
}
