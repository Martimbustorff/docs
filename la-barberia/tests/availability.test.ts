import { describe, expect, it } from "vitest";
import { slotsForDay } from "../src/lib/availability";
import { localTime } from "../src/lib/time";

const base = {
  durationMin: 30,
  stepMin: 15,
  hours: [
    { start: "09:30", end: "13:00" },
    { start: "14:30", end: "19:00" },
  ],
  busy: [],
  now: new Date("2026-10-20T10:00:00Z"),
  minNoticeMin: 0,
  maxDate: "2026-12-31",
};
const times = (d: Date[]) => d.map(localTime);

describe("slotsForDay", () => {
  it("respeita a duração do serviço e a pausa do almoço", () => {
    const t = times(slotsForDay({ ...base, date: "2026-10-21", durationMin: 45 }));
    expect(t[0]).toBe("09:30");
    expect(t).toContain("12:15"); // 12:15 + 45 = 13:00
    expect(t).not.toContain("12:30");
    expect(t.filter((x) => x > "12:15" && x < "14:30")).toEqual([]);
    expect(t).toContain("14:30");
    expect(t.at(-1)).toBe("18:15");
  });

  it("remove horários que se sobrepõem a reservas e bloqueios", () => {
    const busy = [{ start: new Date("2026-10-21T09:45:00Z"), end: new Date("2026-10-21T10:15:00Z") }]; // 10:45-11:15 local
    const t = times(slotsForDay({ ...base, date: "2026-10-21", busy }));
    expect(t).toContain("10:15"); // termina às 10:45, encosta mas não sobrepõe
    expect(t).not.toContain("10:30");
    expect(t).not.toContain("11:00");
    expect(t).toContain("11:15");
  });

  it("aplica a antecedência mínima", () => {
    // agora = 11:00 locais; antecedência 120 min => primeiro início às 13:00 (mas pausa) => 14:30
    const t = times(slotsForDay({ ...base, date: "2026-10-20", minNoticeMin: 120 }));
    expect(t[0]).toBe("14:30");
  });

  it("não devolve dias além do horizonte", () => {
    expect(slotsForDay({ ...base, date: "2026-11-30", maxDate: "2026-11-29" })).toEqual([]);
  });

  it("usa o fuso de Lisboa: 09:30 locais são 08:30Z no verão e 09:30Z no inverno", () => {
    const summer = slotsForDay({ ...base, date: "2026-10-24", hours: [{ start: "09:30", end: "10:30" }] });
    const winter = slotsForDay({ ...base, date: "2026-10-26", hours: [{ start: "09:30", end: "10:30" }] });
    expect(summer[0].toISOString()).toBe("2026-10-24T08:30:00.000Z");
    expect(winter[0].toISOString()).toBe("2026-10-26T09:30:00.000Z");
  });

  it("dia com 25 horas (fim da hora de verão, 25/10/2026): 00:00-06:00 tem 7 horas decorridas", () => {
    const s = slotsForDay({ ...base, date: "2026-10-25", durationMin: 60, stepMin: 60, hours: [{ start: "00:00", end: "06:00" }], now: new Date("2026-10-01T00:00:00Z") });
    expect(s).toHaveLength(7);
    expect(new Set(s.map((d) => d.getTime())).size).toBe(7);
  });

  it("dia com 23 horas (início da hora de verão, 29/03/2026): 00:00-04:00 tem 3 horas decorridas", () => {
    const s = slotsForDay({ ...base, date: "2026-03-29", durationMin: 60, stepMin: 60, hours: [{ start: "00:00", end: "04:00" }], now: new Date("2026-03-01T00:00:00Z") });
    expect(s).toHaveLength(3);
  });
});
