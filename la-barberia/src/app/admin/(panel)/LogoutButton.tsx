"use client";

import { useRouter } from "next/navigation";
import { api } from "../../client-api";

export default function LogoutButton() {
  const router = useRouter();
  return (
    <button
      className="btn secondary small"
      type="button"
      onClick={async () => {
        await api("/api/admin/logout", { body: {} }).catch(() => {});
        router.replace("/admin/login");
        router.refresh();
      }}
    >
      Sair
    </button>
  );
}
