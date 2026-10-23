"use client";

import { useState } from "react";
import { api } from "../../../client-api";

interface S {
  shopName: string; tagline: string; address: string; whatsappE164: string; instagram: string;
  legalName: string; legalNif: string; privacyEmail: string;
  contactConfirmed: boolean; demoMode: boolean; onlineBookingOpen: boolean;
  minNoticeMinutes: number; horizonDays: number; cancelDeadlineHours: number; slotStepMinutes: number; retentionMonths: number;
}

export default function SettingsForm({ initial }: { initial: S }) {
  const [s, setS] = useState<S>(initial);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof S>(k: K, v: S[K]) => setS((p) => ({ ...p, [k]: v }));
  const num = (k: keyof S) => (e: React.ChangeEvent<HTMLInputElement>) => set(k, Number(e.target.value) as never);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null); setMessage(null);
    try {
      const r = await api<{ settings: S }>("/api/admin/settings", { method: "PUT", body: s });
      setS(r.settings);
      setMessage("Definições guardadas.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erro.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="stack" onSubmit={save}>
      <h1 style={{ fontSize: "2rem" }}>Definições</h1>
      <div aria-live="polite">
        {error && <div className="error" role="alert">{error}</div>}
        {message && <div className="ok" role="status">{message}</div>}
      </div>

      <fieldset className="card stack">
        <legend><strong>Dados da barbearia</strong></legend>
        <div className="grid2">
          <div className="field"><label htmlFor="shopName">Nome</label><input id="shopName" value={s.shopName} onChange={(e) => set("shopName", e.target.value)} required /></div>
          <div className="field"><label htmlFor="tagline">Slogan</label><input id="tagline" value={s.tagline} onChange={(e) => set("tagline", e.target.value)} /></div>
          <div className="field"><label htmlFor="address">Morada</label><input id="address" value={s.address} onChange={(e) => set("address", e.target.value)} required /></div>
          <div className="field"><label htmlFor="wa">WhatsApp <span className="hint">Com indicativo, ex.: +351 961 916 045</span></label><input id="wa" value={s.whatsappE164} onChange={(e) => set("whatsappE164", e.target.value)} required /></div>
          <div className="field"><label htmlFor="ig">Instagram <span className="hint">Sem @</span></label><input id="ig" value={s.instagram} onChange={(e) => set("instagram", e.target.value)} /></div>
        </div>
        <label className="check"><input type="checkbox" checked={s.contactConfirmed} onChange={(e) => set("contactConfirmed", e.target.checked)} /> Confirmo que a morada e os contactos acima estão corretos</label>
      </fieldset>

      <fieldset className="card stack">
        <legend><strong>Responsável pelos dados (página de privacidade)</strong></legend>
        <div className="grid2">
          <div className="field"><label htmlFor="legalName">Nome ou firma</label><input id="legalName" value={s.legalName} onChange={(e) => set("legalName", e.target.value)} /></div>
          <div className="field"><label htmlFor="legalNif">NIF</label><input id="legalNif" value={s.legalNif} onChange={(e) => set("legalNif", e.target.value)} /></div>
          <div className="field"><label htmlFor="privacyEmail">Email para pedidos de privacidade</label><input id="privacyEmail" type="email" value={s.privacyEmail} onChange={(e) => set("privacyEmail", e.target.value)} /></div>
        </div>
      </fieldset>

      <fieldset className="card stack">
        <legend><strong>Regras de marcação</strong></legend>
        <label className="check"><input type="checkbox" checked={s.onlineBookingOpen} onChange={(e) => set("onlineBookingOpen", e.target.checked)} /> Marcações online abertas</label>
        <div className="grid2">
          <div className="field"><label htmlFor="notice">Antecedência mínima (minutos)</label><input id="notice" type="number" min={0} max={10080} value={s.minNoticeMinutes} onChange={num("minNoticeMinutes")} /></div>
          <div className="field"><label htmlFor="horizon">Horizonte de marcação (dias)</label><input id="horizon" type="number" min={1} max={365} value={s.horizonDays} onChange={num("horizonDays")} /></div>
          <div className="field"><label htmlFor="deadline">Prazo para cancelar/remarcar online (horas antes)</label><input id="deadline" type="number" min={0} max={168} value={s.cancelDeadlineHours} onChange={num("cancelDeadlineHours")} /></div>
          <div className="field"><label htmlFor="step">Intervalo entre horas oferecidas</label>
            <select id="step" value={s.slotStepMinutes} onChange={(e) => set("slotStepMinutes", Number(e.target.value))}>{[5, 10, 15, 20, 30, 60].map((n) => <option key={n} value={n}>{n} minutos</option>)}</select></div>
          <div className="field"><label htmlFor="ret">Conservação de marcações (meses)</label><input id="ret" type="number" min={1} max={120} value={s.retentionMonths} onChange={num("retentionMonths")} /></div>
        </div>
      </fieldset>

      <fieldset className="card stack">
        <legend><strong>Modo de demonstração</strong></legend>
        <label className="check"><input type="checkbox" checked={s.demoMode} onChange={(e) => set("demoMode", e.target.checked)} /> Mostrar aviso de &quot;dados de demonstração&quot; no site</label>
        <p className="muted small">Desligue quando os serviços, preços e horários forem os reais.</p>
      </fieldset>

      <div><button className="btn" type="submit" disabled={busy}>{busy ? "A guardar…" : "Guardar definições"}</button></div>
    </form>
  );
}
