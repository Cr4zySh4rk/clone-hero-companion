-- Clone Hero Companion cloud schema: profiles, favourites, play history.
-- Every row belongs to one signed-in user; row-level security keeps it private.

create table if not exists public.profiles (
  id uuid primary key references auth.users on delete cascade,
  display_name text,
  avatar_url text,
  ch_username text,            -- linked CH Leaderboards username (optional)
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.favorites (
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  song_key text not null,      -- norm_key("artist - name") + charter, stable across machines
  checksum text,               -- Clone Hero chart MD5 (scorestats "checksum") when known
  artist text, name text, charter text, album text,
  source jsonb not null default '{}'::jsonb,  -- e.g. {"encore_md5": "...", "url": "..."} to reinstall
  added_at timestamptz not null default now(),
  primary key (user_id, song_key)
);

create table if not exists public.plays (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users on delete cascade,
  client_id text not null,     -- dedupe key from the client: checksum + score timestamp
  played_at timestamptz not null,
  checksum text, artist text, name text, charter text,
  instrument text, difficulty text, speed int,
  score int, stars int, accuracy real, notes_hit int, total_notes int, max_streak int,
  fc boolean, completed boolean not null default true,
  duration_s real, machine text,
  raw jsonb,
  unique (user_id, client_id)
);
create index if not exists plays_user_time on public.plays (user_id, played_at desc);
create index if not exists plays_user_song on public.plays (user_id, checksum);

alter table public.profiles enable row level security;
alter table public.favorites enable row level security;
alter table public.plays enable row level security;

create policy "own profile" on public.profiles for all
  using (id = auth.uid()) with check (id = auth.uid());
create policy "own favorites" on public.favorites for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "own plays" on public.plays for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());

create or replace function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.profiles (id, display_name, avatar_url)
  values (new.id,
          coalesce(new.raw_user_meta_data->>'full_name', new.raw_user_meta_data->>'name'),
          new.raw_user_meta_data->>'avatar_url')
  on conflict (id) do nothing;
  return new;
end $$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();
