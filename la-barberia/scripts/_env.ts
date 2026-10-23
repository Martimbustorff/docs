import fs from "node:fs";

// Carrega .env.local / .env (sem sobrepor variáveis já definidas no ambiente).
for (const f of [".env.local", ".env"]) {
  if (fs.existsSync(f)) process.loadEnvFile(f);
}
