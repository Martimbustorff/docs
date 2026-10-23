-- Modelo de dados inicial da plataforma de marcações La barberia.
-- Todas as horas são guardadas em timestamptz (UTC); a conversão para Europe/Lisbon é feita na aplicação.

create extension if not exists btree_gist;

-- Definições da barbearia (uma única linha).
create table settings (
  id                    boolean primary key default true check (id),
  shop_name             text not null default 'La barberia',
  tagline               text not null default 'O clássico nunca sai de moda',
  address               text not null default 'Rua da Vista Alegre, 18A, Cascais',
  whatsapp_e164         text not null default '+351961916045',
  instagram             text not null default 'la_barberiabycarlos',
  -- Dados do responsável pelo tratamento (página de privacidade). Têm de ser preenchidos antes de publicar.
  legal_name            text not null default '',
  legal_nif             text not null default '',
  privacy_email         text not null default '',
  -- Enquanto for false, o painel avisa que a morada/contactos ainda não foram confirmados.
  contact_confirmed     boolean not null default false,
  -- Enquanto for true, o site mostra um aviso de "dados de demonstração".
  demo_mode             boolean not null default true,
  online_booking_open   boolean not null default true,
  min_notice_minutes    integer not null default 120 check (min_notice_minutes between 0 and 10080),
  horizon_days          integer not null default 45  check (horizon_days between 1 and 365),
  cancel_deadline_hours integer not null default 4   check (cancel_deadline_hours between 0 and 168),
  slot_step_minutes     integer not null default 15  check (slot_step_minutes in (5, 10, 15, 20, 30, 60)),
  retention_months      integer not null default 24  check (retention_months between 1 and 120),
  updated_at            timestamptz not null default now()
);
insert into settings default values;

create table staff (
  id         uuid primary key default gen_random_uuid(),
  name       text not null check (length(btrim(name)) between 1 and 60),
  active     boolean not null default true,
  sort_order integer not null default 0,
  created_at timestamptz not null default now()
);

create table services (
  id           uuid primary key default gen_random_uuid(),
  name         text not null check (length(btrim(name)) between 1 and 80),
  description  text not null default '',
  price_cents  integer not null check (price_cents >= 0 and price_cents <= 100000),
  duration_min integer not null check (duration_min between 5 and 480),
  active       boolean not null default true,
  sort_order   integer not null default 0,
  is_demo      boolean not null default false,
  created_at   timestamptz not null default now()
);

-- Horário semanal por profissional. weekday segue ISO: 1 = segunda ... 7 = domingo.
-- Várias linhas por dia permitem pausas (por exemplo 09:30-13:00 e 14:30-19:00).
create table working_hours (
  id         uuid primary key default gen_random_uuid(),
  staff_id   uuid not null references staff(id) on delete cascade,
  weekday    smallint not null check (weekday between 1 and 7),
  start_time time not null,
  end_time   time not null,
  check (end_time > start_time),
  exclude using gist (
    staff_id with =,
    weekday with =,
    tsrange(date '2000-01-01' + start_time, date '2000-01-01' + end_time) with &&
  )
);

-- Fonte única de verdade para marcações E bloqueios (férias, feriados, pausas pontuais).
-- A restrição de exclusão no fim impede, ao nível da base de dados, qualquer sobreposição
-- para o mesmo profissional, mesmo com pedidos simultâneos.
create table calendar_entries (
  id               uuid primary key default gen_random_uuid(),
  kind             text not null check (kind in ('appointment', 'block')),
  staff_id         uuid not null references staff(id),
  starts_at        timestamptz not null,
  ends_at          timestamptz not null,
  status           text not null default 'confirmed'
                     check (status in ('confirmed', 'completed', 'no_show', 'cancelled')),

  -- Bloqueios
  block_kind       text check (block_kind in ('block', 'vacation', 'holiday')),
  block_group      uuid,
  reason           text,

  -- Marcações
  service_id       uuid references services(id),
  service_name     text,
  price_cents      integer,
  duration_min     integer,
  customer_name    text,
  customer_phone   text,
  customer_email   text,
  notes            text,
  source           text check (source in ('online', 'phone', 'whatsapp', 'walk_in')),
  token_hash       bytea unique,
  idempotency_key  text unique,
  reschedule_count integer not null default 0,
  cancelled_at     timestamptz,
  cancelled_by     text check (cancelled_by in ('customer', 'admin')),

  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),

  check (ends_at > starts_at),
  check (kind <> 'appointment' or (
    service_id is not null and customer_name is not null
    and customer_phone is not null and source is not null
  )),
  check (kind <> 'block' or block_kind is not null),

  constraint calendar_entries_no_overlap exclude using gist (
    staff_id with =,
    tstzrange(starts_at, ends_at, '[)') with &&
  ) where (status <> 'cancelled')
);
create index calendar_entries_starts_at_idx on calendar_entries (starts_at);
create index calendar_entries_phone_idx on calendar_entries (customer_phone) where kind = 'appointment';

create table admins (
  id              uuid primary key default gen_random_uuid(),
  email           text not null unique check (email = lower(email)),
  password_hash   text not null,
  failed_attempts integer not null default 0,
  locked_until    timestamptz,
  created_at      timestamptz not null default now()
);

create table admin_sessions (
  token_hash bytea primary key,
  admin_id   uuid not null references admins(id) on delete cascade,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index admin_sessions_expires_idx on admin_sessions (expires_at);

-- Limitação de pedidos (janela fixa). A chave contém apenas um hash do IP, nunca o IP.
create table rate_limits (
  key          text primary key,
  window_start timestamptz not null,
  hits         integer not null
);

-- Segurança: as tabelas só são acedidas pelo servidor da aplicação (ligação direta ao Postgres).
-- Ativar RLS sem políticas garante que a API pública do Supabase (PostgREST), se existir, não lê nada.
alter table settings         enable row level security;
alter table staff            enable row level security;
alter table services         enable row level security;
alter table working_hours    enable row level security;
alter table calendar_entries enable row level security;
alter table admins           enable row level security;
alter table admin_sessions   enable row level security;
alter table rate_limits      enable row level security;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on all tables in schema public from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on all tables in schema public from authenticated';
  end if;
end $$;
