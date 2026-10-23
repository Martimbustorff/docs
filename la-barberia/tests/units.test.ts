import { describe, expect, it } from "vitest";
import { csvCell, toCsv } from "../src/lib/csv";
import { buildIcs } from "../src/lib/ics";
import { normalizePhone } from "../src/lib/validation";
import { parseLocalDateTime, localDateTime } from "../src/lib/time";

describe("telemóveis", () => {
  it("normaliza formatos portugueses e internacionais", () => {
    expect(normalizePhone("912 345 678")).toBe("+351912345678");
    expect(normalizePhone("+351 912 345 678")).toBe("+351912345678");
    expect(normalizePhone("00351912345678")).toBe("+351912345678");
    expect(normalizePhone("+44 7700 900123")).toBe("+447700900123");
    expect(normalizePhone("123")).toBeNull();
    expect(normalizePhone("812345678")).toBeNull();
    expect(normalizePhone("abc")).toBeNull();
  });
});

describe("CSV", () => {
  it("neutraliza fórmulas e escapa separadores", () => {
    expect(csvCell("=HYPERLINK(\"x\")")).toBe("\"'=HYPERLINK(\"\"x\"\")\"");
    expect(csvCell("+351912345678")).toBe("'+351912345678");
    expect(csvCell("a;b")).toBe("\"a;b\"");
    expect(csvCell(null)).toBe("");
    expect(toCsv(["a", "b"], [["1", "2"]]).startsWith("﻿a;b\r\n1;2")).toBe(true);
  });
});

describe("ICS", () => {
  it("gera um evento válido em UTC com CRLF", () => {
    const ics = buildIcs(
      { id: "abc", startsAt: new Date("2026-10-21T09:30:00Z"), endsAt: new Date("2026-10-21T10:00:00Z"), serviceName: "Corte, barba; extra" },
      { shopName: "La barberia", address: "Rua da Vista Alegre, 18A, Cascais" },
      new Date("2026-10-20T10:00:00Z"),
    );
    expect(ics).toContain("BEGIN:VCALENDAR\r\n");
    expect(ics).toContain("DTSTART:20261021T093000Z\r\n");
    expect(ics).toContain("DTEND:20261021T100000Z\r\n");
    expect(ics).toContain("Corte\\, barba\\; extra");
    expect(ics.endsWith("END:VCALENDAR\r\n")).toBe(true);
    expect(ics).not.toMatch(/(?<!\r)\n/);
  });
});

describe("hora local", () => {
  it("converte hora de Lisboa e rejeita horas inexistentes na mudança de hora", () => {
    expect(parseLocalDateTime("2026-10-21T10:30")?.toISOString()).toBe("2026-10-21T09:30:00.000Z");
    expect(parseLocalDateTime("2026-01-21T10:30")?.toISOString()).toBe("2026-01-21T10:30:00.000Z");
    expect(parseLocalDateTime("2026-03-29T01:30")).toBeNull(); // 01:00-02:00 não existe nesse dia
    expect(parseLocalDateTime("2026-13-40T10:30")).toBeNull();
    expect(localDateTime(new Date("2026-10-21T09:30:00Z"))).toBe("2026-10-21T10:30");
  });
});
