import { NextRequest, NextResponse } from "next/server";
import { ZodError, type ZodType } from "zod";
import { ApiError, Errors } from "./errors";
import { hmacShort } from "./tokens";

type Handler<C> = (req: NextRequest, ctx: C) => Promise<Response | Record<string, unknown> | unknown[]>;

/** Envolve um handler: converte erros conhecidos em respostas JSON em português, sem expor detalhes internos. */
export function route<C = unknown>(fn: Handler<C>) {
  return async (req: NextRequest, ctx: C): Promise<Response> => {
    try {
      const out = await fn(req, ctx);
      return out instanceof Response ? out : NextResponse.json(out);
    } catch (e) {
      if (e instanceof ApiError) {
        return NextResponse.json({ error: { code: e.code, message: e.message } }, { status: e.status });
      }
      if (e instanceof ZodError) {
        const msg = e.issues[0]?.message;
        return NextResponse.json(
          { error: { code: "VALIDATION", message: msg && !/^Invalid/i.test(msg) ? msg : Errors.validation().message } },
          { status: 400 },
        );
      }
      console.error("[api] erro inesperado", e);
      return NextResponse.json({ error: { code: "INTERNAL", message: "Ocorreu um erro. Tente novamente dentro de instantes." } }, { status: 500 });
    }
  };
}

export async function readJson<T>(req: NextRequest, schema: ZodType<T>): Promise<T> {
  const ct = req.headers.get("content-type") ?? "";
  if (!ct.toLowerCase().startsWith("application/json")) throw Errors.validation("Formato de pedido inválido.");
  const text = await req.text();
  if (text.length > 32_768) throw Errors.validation("Pedido demasiado grande.");
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw Errors.validation("JSON inválido.");
  }
  return schema.parse(body);
}

/** Hash do IP do cliente (o IP nunca é guardado). Atrás de um proxy de confiança usa X-Forwarded-For. */
export function clientIpHash(req: NextRequest): string {
  const fwd = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim();
  const ip = fwd || req.headers.get("x-real-ip") || "unknown";
  return hmacShort(ip);
}
