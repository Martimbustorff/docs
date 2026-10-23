function required(name: string): string {
  const v = process.env[name];
  if (!v || !v.trim()) throw new Error(`Variável de ambiente em falta: ${name}. Veja .env.example.`);
  return v.trim();
}

export function env() {
  const appSecret = required("APP_SECRET");
  if (appSecret.length < 32) throw new Error("APP_SECRET tem de ter pelo menos 32 caracteres.");
  return {
    DATABASE_URL: required("DATABASE_URL"),
    APP_SECRET: appSecret,
    PUBLIC_BASE_URL: (process.env.PUBLIC_BASE_URL?.trim() || "http://localhost:3000").replace(/\/+$/, ""),
    CRON_SECRET: process.env.CRON_SECRET?.trim() || "",
    NOTIFY_PROVIDER: process.env.NOTIFY_PROVIDER?.trim().toLowerCase() || "",
    CALLMEBOT_PHONE: process.env.CALLMEBOT_PHONE?.trim() || "",
    CALLMEBOT_APIKEY: process.env.CALLMEBOT_APIKEY?.trim() || "",
    NOTIFY_WEBHOOK_URL: process.env.NOTIFY_WEBHOOK_URL?.trim() || "",
    IS_PROD: process.env.NODE_ENV === "production",
  };
}
