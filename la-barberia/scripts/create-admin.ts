import "./_env";
import postgres from "postgres";
import { hashPassword } from "../src/lib/tokens";

/**
 * Cria (ou, com --reset, repõe a palavra-passe de) um administrador. Não existe registo público.
 * Uso:   npm run admin:create -- carlos@exemplo.pt
 * A palavra-passe é pedida sem eco no terminal (ou lida de ADMIN_PASSWORD em ambientes não interativos).
 */
function promptHidden(label: string): Promise<string> {
  return new Promise((resolve) => {
    process.stdout.write(label);
    const stdin = process.stdin;
    stdin.setRawMode(true);
    stdin.resume();
    stdin.setEncoding("utf8");
    let value = "";
    const onData = (ch: string) => {
      for (const c of ch) {
        if (c === "\r" || c === "\n" || c === "\u0004") {
          stdin.setRawMode(false);
          stdin.pause();
          stdin.off("data", onData);
          process.stdout.write("\n");
          return resolve(value);
        }
        if (c === "\u0003") process.exit(130);
        if (c === "\u007f") value = value.slice(0, -1);
        else value += c;
      }
    };
    stdin.on("data", onData);
  });
}

const args = process.argv.slice(2);
const reset = args.includes("--reset");
const email = args.find((a) => !a.startsWith("--"))?.trim().toLowerCase();
if (!email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
  console.error("Uso: npm run admin:create -- <email> [--reset]");
  process.exit(1);
}
if (!process.env.DATABASE_URL) {
  console.error("Defina DATABASE_URL.");
  process.exit(1);
}

async function main() {
let password = process.env.ADMIN_PASSWORD ?? "";
if (!password) {
  if (!process.stdin.isTTY) {
    console.error("Sem terminal interativo: defina ADMIN_PASSWORD.");
    process.exit(1);
  }
  password = await promptHidden("Palavra-passe (mín. 12 caracteres): ");
  const again = await promptHidden("Repita a palavra-passe: ");
  if (password !== again) {
    console.error("As palavras-passe não coincidem.");
    process.exit(1);
  }
}
if (password.length < 12) {
  console.error("A palavra-passe tem de ter pelo menos 12 caracteres.");
  process.exit(1);
}

const sql = postgres(process.env.DATABASE_URL!, { max: 1, onnotice: () => {} });
try {
  const hash = await hashPassword(password);
  const [existing] = await sql`select id from admins where email = ${email!}`;
  if (existing && !reset) {
    console.error("Já existe um administrador com esse email. Use --reset para repor a palavra-passe.");
    process.exit(1);
  }
  if (existing) {
    await sql`update admins set password_hash = ${hash}, failed_attempts = 0, locked_until = null where id = ${existing.id}`;
    await sql`delete from admin_sessions where admin_id = ${existing.id}`;
    console.log("Palavra-passe reposta e sessões antigas terminadas.");
  } else {
    await sql`insert into admins (email, password_hash) values (${email!}, ${hash})`;
    console.log(`Administrador criado: ${email}`);
  }
} finally {
  await sql.end({ timeout: 5 });
}
}
main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
