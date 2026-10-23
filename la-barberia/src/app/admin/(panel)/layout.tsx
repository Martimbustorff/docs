import Link from "next/link";
import { db } from "@/lib/db";
import { getSettings } from "@/lib/data";
import { requireAdminPage } from "@/lib/session";
import LogoutButton from "./LogoutButton";

export const dynamic = "force-dynamic";

export default async function PanelLayout({ children }: { children: React.ReactNode }) {
  const admin = await requireAdminPage();
  const settings = await getSettings(db());
  return (
    <>
      <header className="admin-header">
        <div className="wrap">
          <strong style={{ fontFamily: "var(--serif)", fontSize: "1.2rem", marginRight: ".5rem" }}>La barberia</strong>
          <nav aria-label="Painel" className="row" style={{ gap: "1rem" }}>
            <Link href="/admin">Agenda</Link>
            <Link href="/admin/servicos">Serviços</Link>
            <Link href="/admin/equipa">Equipa e horários</Link>
            <Link href="/admin/definicoes">Definições</Link>
            <Link href="/admin/conta">Conta</Link>
          </nav>
          <span style={{ flex: 1 }} />
          <span className="small" style={{ opacity: 0.8 }}>{admin.email}</span>
          <LogoutButton />
        </div>
      </header>
      <main id="conteudo" className="admin-main">
        <div className="wrap stack">
          {!settings.contactConfirmed && (
            <div className="error" role="note">
              <strong>Antes de publicar:</strong> confirme a morada, o WhatsApp e os dados de privacidade em <Link href="/admin/definicoes">Definições</Link>.
            </div>
          )}
          {settings.demoMode && (
            <div className="notice" role="note">
              O site mostra o aviso de <strong>dados de demonstração</strong>. Edite os serviços e horários e desligue o modo de demonstração em <Link href="/admin/definicoes">Definições</Link>.
            </div>
          )}
          {children}
        </div>
      </main>
    </>
  );
}
