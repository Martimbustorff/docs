import { redirect } from "next/navigation";
import { currentAdmin } from "@/lib/session";
import LoginForm from "./LoginForm";

export const dynamic = "force-dynamic";

export default async function LoginPage() {
  if (await currentAdmin()) redirect("/admin");
  return (
    <main id="conteudo" className="wrap narrow stack" style={{ padding: "3rem 1rem" }}>
      <h1 style={{ fontSize: "2rem" }}>Painel La barberia</h1>
      <LoginForm />
    </main>
  );
}
