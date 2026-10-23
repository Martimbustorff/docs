"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiClientError, api, dayParts, longLisbon, shiftDate } from "../client-api";

interface Service { id: string; name: string; description: string; durationMin: number }
interface Staff { id: string; name: string }
interface Slot { startsAt: string; label: string }
type Days = Record<string, Slot[]>;
type StepId = "service" | "staff" | "time" | "details" | "review";

const LABELS: Record<StepId, string> = {
  service: "Serviço",
  staff: "Profissional",
  time: "Data e hora",
  details: "Os seus dados",
  review: "Confirmar",
};

const newKey = () => crypto.randomUUID().replace(/-/g, "") + Math.random().toString(36).slice(2, 8);

export default function Wizard(props: {
  services: Service[];
  staff: Staff[];
  today: string;
  maxDate: string;
  shop: { whatsappLink: string; cancelDeadlineHours: number };
}) {
  const { services, staff, today, maxDate, shop } = props;
  const router = useRouter();
  // Com um só serviço (o caso normal) o passo "Serviço" não aparece: dia, hora, dados, confirmar.
  const steps: StepId[] = [...(services.length > 1 ? (["service"] as StepId[]) : []), ...(staff.length > 1 ? (["staff"] as StepId[]) : []), "time", "details", "review"];

  const [step, setStep] = useState<StepId>(steps[0]);
  const [serviceId, setServiceId] = useState<string>(services.length === 1 ? services[0].id : "");
  const [staffId, setStaffId] = useState<string>(staff.length > 1 ? "any" : staff[0].id);
  const [days, setDays] = useState<Days | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [date, setDate] = useState<string>("");
  const [slot, setSlot] = useState<Slot | null>(null);
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [website, setWebsite] = useState(""); // campo isco
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{ name?: string; phone?: string }>({});
  const keyRef = useRef<string>("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const submittingRef = useRef(false);

  useEffect(() => {
    keyRef.current = newKey();
  }, []);
  useEffect(() => {
    headingRef.current?.focus();
  }, [step]);

  const service = services.find((s) => s.id === serviceId);
  const staffName = staffId === "any" ? "Sem preferência" : staff.find((s) => s.id === staffId)?.name ?? "";

  // Carrega as vagas reais sempre que se entra no passo "Data e hora" ou muda o serviço/profissional.
  useEffect(() => {
    if (step !== "time" || !serviceId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setLoadError(null);
      try {
        const merged: Days = {};
        let from = today;
        while (from <= maxDate) {
          const to = shiftDate(from, 61) < maxDate ? shiftDate(from, 61) : maxDate;
          const r = await api<{ days: Days; closed: boolean }>(
            `/api/public/availability?serviceId=${serviceId}&staffId=${staffId}&from=${from}&to=${to}`,
          );
          Object.assign(merged, r.days);
          from = shiftDate(to, 1);
        }
        if (cancelled) return;
        setDays(merged);
        const first = Object.keys(merged).sort()[0] ?? "";
        setDate((d) => (d && merged[d] ? d : first));
      } catch (e) {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : "Não foi possível carregar as vagas.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [step, serviceId, staffId, today, maxDate, tick]);

  const idx = steps.indexOf(step);
  const next = () => setStep(steps[Math.min(idx + 1, steps.length - 1)]);
  const back = () => setStep(steps[Math.max(idx - 1, 0)]);

  function validateDetails(): boolean {
    const errs: typeof fieldErrors = {};
    if (name.trim().length < 2) errs.name = "Indique o seu nome.";
    if (phone.replace(/\D/g, "").length < 9) errs.phone = "Indique um número de telemóvel válido.";
    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  }

  async function submit() {
    if (submittingRef.current || !slot || !service) return;
    submittingRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api<{ token: string }>("/api/public/bookings", {
        body: {
          serviceId,
          staffId,
          startsAt: slot.startsAt,
          name: name.trim(),
          phone: phone.trim(),
          idempotencyKey: keyRef.current,
          website,
        },
      });
      router.push(`/marcacao/${res.token}?novo=1`);
    } catch (e) {
      const err = e instanceof ApiClientError ? e : null;
      if (err?.code === "SLOT_TAKEN") {
        setError(err.message);
        keyRef.current = newKey();
        setSlot(null);
        setStep("time");
      } else if (err?.code === "NETWORK") {
        // Mantemos a mesma chave: repetir o pedido não cria uma marcação duplicada.
        setError(`${err.message} Pode tentar de novo: não será criada uma marcação duplicada.`);
      } else {
        setError(e instanceof Error ? e.message : "Ocorreu um erro. Tente novamente.");
      }
      setSubmitting(false);
      submittingRef.current = false;
    }
  }

  const dayList = days ? Object.keys(days).sort() : [];

  return (
    <div>
      <ol className="steps" aria-label="Passos da marcação">
        {steps.map((s, i) => (
          <li key={s} aria-current={s === step ? "step" : undefined} className={i < idx ? "done" : ""}>
            {LABELS[s]}
          </li>
        ))}
      </ol>

      <div aria-live="polite">
        {error && (
          <div className="error" role="alert" style={{ marginBottom: "1rem" }}>
            {error}
          </div>
        )}
      </div>

      {step === "service" && (
        <section className="stack" aria-labelledby="h-service">
          <h2 id="h-service" ref={headingRef} tabIndex={-1}>Escolha o serviço</h2>
          <div className="options">
            {services.map((s) => (
              <button key={s.id} type="button" className="option" aria-pressed={serviceId === s.id} onClick={() => { setServiceId(s.id); setSlot(null); setDays(null); }}>
                <span>
                  <span className="name">{s.name}</span>
                  <span className="meta">{s.durationMin} min{s.description ? ` · ${s.description}` : ""}</span>
                </span>
              </button>
            ))}
          </div>
          <div className="row"><button className="btn" type="button" disabled={!serviceId} onClick={next}>Continuar</button></div>
        </section>
      )}

      {step === "staff" && (
        <section className="stack" aria-labelledby="h-staff">
          <h2 id="h-staff" ref={headingRef} tabIndex={-1}>Escolha o profissional</h2>
          <div className="options">
            {[{ id: "any", name: "Sem preferência" }, ...staff].map((s) => (
              <button key={s.id} type="button" className="option" aria-pressed={staffId === s.id} onClick={() => { setStaffId(s.id); setSlot(null); setDays(null); }}>
                <span className="name">{s.name}</span>
              </button>
            ))}
          </div>
          <div className="row">
            <button className="btn secondary" type="button" onClick={back}>Voltar</button>
            <button className="btn" type="button" onClick={next}>Continuar</button>
          </div>
        </section>
      )}

      {step === "time" && (
        <section className="stack" aria-labelledby="h-time">
          <h2 id="h-time" ref={headingRef} tabIndex={-1}>Escolha o dia e a hora</h2>
          <p className="muted small">Horas de Lisboa. Só mostramos vagas realmente livres.</p>
          {loading && <p role="status">A carregar vagas…</p>}
          {loadError && (
            <div className="error" role="alert">
              {loadError} <button className="btn small secondary" type="button" onClick={() => setTick((t) => t + 1)}>Tentar novamente</button>
            </div>
          )}
          {!loading && !loadError && days && dayList.length === 0 && (
            <div className="notice">
              <p>Não há vagas nos próximos dias para esta escolha. Experimente outro profissional ou contacte-nos.</p>
              <a className="btn" href={shop.whatsappLink}>Contactar por WhatsApp</a>
            </div>
          )}
          {!loading && dayList.length > 0 && (
            <>
              <div className="days" role="group" aria-label="Dias com vagas">
                {dayList.map((d) => {
                  const p = dayParts(d);
                  return (
                    <button key={d} type="button" className="day" aria-pressed={date === d} aria-label={p.long} onClick={() => { setDate(d); setSlot(null); }}>
                      <span className="wd">{p.weekday}</span>
                      <span className="dn">{p.day}</span>
                      <span className="mn">{p.month}</span>
                    </button>
                  );
                })}
              </div>
              {date && days?.[date] && (
                <div role="group" aria-label={`Horas disponíveis em ${dayParts(date).long}`}>
                  <h3 className="cap">{dayParts(date).long}</h3>
                  <div className="times">
                    {days[date].map((s) => (
                      <button key={s.startsAt} type="button" className="time" aria-pressed={slot?.startsAt === s.startsAt} onClick={() => setSlot(s)}>
                        {s.label}
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
          <div className="row">
            <button className="btn secondary" type="button" onClick={back}>Voltar</button>
            <button className="btn" type="button" disabled={!slot} onClick={next}>Continuar</button>
          </div>
        </section>
      )}

      {step === "details" && (
        <form
          className="stack"
          aria-labelledby="h-details"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            if (validateDetails()) next();
          }}
        >
          <h2 id="h-details" ref={headingRef} tabIndex={-1}>Os seus dados</h2>
          <div className="field">
            <label htmlFor="name">Nome</label>
            <input id="name" name="name" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!fieldErrors.name} aria-describedby={fieldErrors.name ? "e-name" : undefined} maxLength={80} />
            {fieldErrors.name && <span id="e-name" className="field-error">{fieldErrors.name}</span>}
          </div>
          <div className="field">
            <label htmlFor="phone">Telemóvel <span className="hint">Só para contactarmos se for necessário.</span></label>
            <input id="phone" name="phone" type="tel" inputMode="tel" autoComplete="tel" value={phone} onChange={(e) => setPhone(e.target.value)} aria-invalid={!!fieldErrors.phone} aria-describedby={fieldErrors.phone ? "e-phone" : undefined} maxLength={30} />
            {fieldErrors.phone && <span id="e-phone" className="field-error">{fieldErrors.phone}</span>}
          </div>
          <div className="trap" aria-hidden="true">
            <label htmlFor="website">Não preencha este campo</label>
            <input id="website" name="website" tabIndex={-1} autoComplete="off" value={website} onChange={(e) => setWebsite(e.target.value)} />
          </div>
          <div className="row">
            <button className="btn secondary" type="button" onClick={back}>Voltar</button>
            <button className="btn" type="submit">Continuar</button>
          </div>
        </form>
      )}

      {step === "review" && service && slot && (
        <section className="stack" aria-labelledby="h-review">
          <h2 id="h-review" ref={headingRef} tabIndex={-1}>Reveja e confirme</h2>
          <dl className="summary card">
            <dt>Serviço</dt><dd>{service.name} · {service.durationMin} min</dd>
            {staff.length > 1 && (<><dt>Profissional</dt><dd>{staffName}</dd></>)}
            <dt>Quando</dt><dd className="cap">{longLisbon(slot.startsAt)}</dd>
            <dt>Nome</dt><dd>{name.trim()}</dd>
            <dt>Telemóvel</dt><dd>{phone.trim()}</dd>
          </dl>
          <p className="muted small">
            Poderá cancelar ou remarcar online até {shop.cancelDeadlineHours} h antes, através de um link privado que receberá no ecrã seguinte. Ao confirmar, aceita que tratemos estes dados apenas para gerir a marcação (ver <Link href="/privacidade">Privacidade</Link>).
          </p>
          <div className="row">
            <button className="btn secondary" type="button" onClick={back} disabled={submitting}>Voltar</button>
            <button className="btn" type="button" onClick={submit} disabled={submitting} aria-busy={submitting}>
              {submitting ? "A confirmar…" : "Confirmar marcação"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
