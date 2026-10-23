import Link from "next/link";
import type { Settings } from "@/lib/types";

export function DemoBanner({ settings }: { settings: Pick<Settings, "demoMode"> }) {
  if (!settings.demoMode) return null;
  return (
    <div className="demo-banner" role="note">
      Versão de demonstração: o horário é provisório.
    </div>
  );
}

export function SiteHeader({ settings, open }: { settings: Pick<Settings, "shopName">; open: boolean }) {
  return (
    <header className="site-header">
      <div className="wrap">
        <Link className="brand" href="/">{settings.shopName}</Link>
        {open && <Link className="btn small" href="/marcar">Marcar</Link>}
      </div>
    </header>
  );
}
