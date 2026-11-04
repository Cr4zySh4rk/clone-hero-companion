-- Clone Hero Companion 2.3.0: usernames, friend codes, friends, setlists in the cloud, and per-list sharing.
-- Idempotent and safe to run as ONE batch (Management API /database/query). No CONCURRENTLY, no psql meta-commands.
--
-- Privacy model: every table below is private to its owner (RLS) except where a policy explicitly lets an
-- ACCEPTED friend read a list the owner switched on. Email addresses live in auth.users and are never exposed:
-- profiles has none, and the lookup functions return only username, display name and avatar.

-- ======================================================================== profiles: username + friend code
alter table public.profiles add column if not exists username text;
alter table public.profiles add column if not exists friend_code text;

-- 8 characters from an alphabet without look-alikes (no 0/O, 1/I/L). Retries until unused.
create or replace function public.gen_friend_code() returns text
language plpgsql volatile set search_path = '' as $$
declare
  alphabet constant text := 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
  code text;
  i int;
begin
  loop
    code := '';
    for i in 1..8 loop
      code := code || substr(alphabet, 1 + floor(random() * length(alphabet))::int, 1);
    end loop;
    exit when not exists (select 1 from public.profiles p where p.friend_code = code);
  end loop;
  return code;
end $$;

update public.profiles set friend_code = public.gen_friend_code() where friend_code is null;
alter table public.profiles alter column friend_code set default public.gen_friend_code();
alter table public.profiles alter column friend_code set not null;

alter table public.profiles drop constraint if exists profiles_username_format;
alter table public.profiles add constraint profiles_username_format
  check (username is null or username ~ '^[a-z0-9_]{3,20}$');
alter table public.profiles drop constraint if exists profiles_friend_code_format;
alter table public.profiles add constraint profiles_friend_code_format
  check (friend_code ~ '^[A-Z0-9]{8}$');

create unique index if not exists profiles_username_key on public.profiles (lower(username));
create unique index if not exists profiles_friend_code_key on public.profiles (friend_code);

-- The friend code is assigned once: an owner updating their own profile cannot change or clear it.
create or replace function public.profiles_guard() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.friend_code := old.friend_code;
  new.username := lower(new.username);
  new.updated_at := now();
  return new;
end $$;
drop trigger if exists profiles_guard on public.profiles;
create trigger profiles_guard before update on public.profiles
  for each row execute function public.profiles_guard();

-- Lower-case the username on insert too (the handle_new_user trigger inserts without one).
create or replace function public.profiles_lower() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.username := lower(new.username);
  return new;
end $$;
drop trigger if exists profiles_lower on public.profiles;
create trigger profiles_lower before insert on public.profiles
  for each row execute function public.profiles_lower();

-- ======================================================================== public lookup (no email, no ids beyond what a request needs)
-- Look a person up by username or friend code. Signed-in users only.
create or replace function public.lookup_profile(q text)
returns table (username text, display_name text, avatar_url text)
language sql stable security definer set search_path = '' as $$
  select p.username, p.display_name, p.avatar_url
  from public.profiles p
  where auth.uid() is not null
    and p.id <> auth.uid()
    and ( (p.username is not null and p.username = lower(btrim(q)))
       or p.friend_code = upper(btrim(q)) )
  limit 1
$$;

create or replace function public.username_available(name text)
returns boolean
language sql stable security definer set search_path = '' as $$
  select auth.uid() is not null
     and lower(btrim(name)) ~ '^[a-z0-9_]{3,20}$'
     and not exists (select 1 from public.profiles p
                     where p.username = lower(btrim(name)) and p.id <> auth.uid())
$$;

-- ======================================================================== friendships
create table if not exists public.friendships (
  requester uuid not null default auth.uid() references auth.users on delete cascade,
  addressee uuid not null references auth.users on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'accepted')),
  created_at timestamptz not null default now(),
  primary key (requester, addressee),
  check (requester <> addressee)
);
-- one row per pair, whichever way round
create unique index if not exists friendships_pair_key on public.friendships
  (least(requester, addressee), greatest(requester, addressee));
create index if not exists friendships_addressee on public.friendships (addressee, status);

alter table public.friendships enable row level security;

drop policy if exists "friendships: parties read" on public.friendships;
create policy "friendships: parties read" on public.friendships for select
  using (requester = auth.uid() or addressee = auth.uid());
drop policy if exists "friendships: requester inserts" on public.friendships;
create policy "friendships: requester inserts" on public.friendships for insert
  with check (requester = auth.uid() and status = 'pending');
drop policy if exists "friendships: addressee accepts" on public.friendships;
create policy "friendships: addressee accepts" on public.friendships for update
  using (addressee = auth.uid() and status = 'pending')
  with check (addressee = auth.uid() and status = 'accepted');
drop policy if exists "friendships: parties delete" on public.friendships;
create policy "friendships: parties delete" on public.friendships for delete
  using (requester = auth.uid() or addressee = auth.uid());

-- An update may only flip pending -> accepted; the parties and the date never change.
create or replace function public.friendships_guard() returns trigger
language plpgsql set search_path = '' as $$
begin
  if new.requester <> old.requester or new.addressee <> old.addressee or new.created_at <> old.created_at
     or old.status <> 'pending' or new.status <> 'accepted' then
    raise exception 'only pending -> accepted is allowed' using errcode = '42501';
  end if;
  return new;
end $$;
drop trigger if exists friendships_guard on public.friendships;
create trigger friendships_guard before update on public.friendships
  for each row execute function public.friendships_guard();

-- Are a and b accepted friends? (security definer so policies on other tables can call it)
create or replace function public.are_friends(a uuid, b uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select a is not null and b is not null and exists (
    select 1 from public.friendships f
    where f.status = 'accepted'
      and ((f.requester = a and f.addressee = b) or (f.requester = b and f.addressee = a)))
$$;

-- Send a request by username or friend code. If that person already asked me, this accepts it.
-- Returns 'pending' or 'accepted'; raises 'not_found' / 'self' / 'exists'.
create or replace function public.request_friend(q text) returns text
language plpgsql security definer set search_path = '' as $$
declare
  me uuid := auth.uid();
  other uuid;
  existing public.friendships;
begin
  if me is null then raise exception 'not signed in' using errcode = '28000'; end if;
  select p.id into other from public.profiles p
   where (p.username is not null and p.username = lower(btrim(q))) or p.friend_code = upper(btrim(q)) limit 1;
  if other is null then raise exception 'not_found' using errcode = 'P0002'; end if;
  if other = me then raise exception 'self' using errcode = 'P0001'; end if;
  select * into existing from public.friendships f
   where (f.requester = me and f.addressee = other) or (f.requester = other and f.addressee = me);
  if found then
    if existing.status = 'pending' and existing.requester = other then
      update public.friendships set status = 'accepted' where requester = other and addressee = me;
      return 'accepted';
    end if;
    raise exception 'exists' using errcode = '23505';
  end if;
  insert into public.friendships (requester, addressee, status) values (me, other, 'pending');
  return 'pending';
end $$;

-- My friendships with the other person's public face. Never returns an email.
create or replace function public.my_friendships()
returns table (other_id uuid, direction text, status text, created_at timestamptz,
               username text, display_name text, avatar_url text)
language sql stable security definer set search_path = '' as $$
  select p.id,
         case when f.requester = auth.uid() then 'outgoing' else 'incoming' end,
         f.status, f.created_at, p.username, p.display_name, p.avatar_url
  from public.friendships f
  join public.profiles p on p.id = case when f.requester = auth.uid() then f.addressee else f.requester end
  where auth.uid() is not null and (f.requester = auth.uid() or f.addressee = auth.uid())
  order by f.created_at desc
$$;

-- ======================================================================== sharing switches
-- Private by default. A separate table keeps the profile row (which friends never read) free of it.
create table if not exists public.share_settings (
  user_id uuid primary key default auth.uid() references auth.users on delete cascade,
  favorites_shared boolean not null default false,
  updated_at timestamptz not null default now()
);
alter table public.share_settings enable row level security;
drop policy if exists "own share settings" on public.share_settings;
create policy "own share settings" on public.share_settings for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());

-- May the signed-in user read owner's favourites? (friends AND switched on). Security definer so the friend
-- does not need read access to the owner's share_settings row.
create or replace function public.favorites_visible(owner uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select owner = auth.uid()
      or (public.are_friends(auth.uid(), owner)
          and coalesce((select s.favorites_shared from public.share_settings s where s.user_id = owner), false))
$$;

drop policy if exists "friends read shared favorites" on public.favorites;
create policy "friends read shared favorites" on public.favorites for select
  using (user_id <> auth.uid() and public.favorites_visible(user_id));

-- ======================================================================== setlists
create table if not exists public.setlists (
  id uuid primary key default gen_random_uuid(),
  owner uuid not null default auth.uid() references auth.users on delete cascade,
  name text not null check (char_length(name) between 1 and 100),
  shared boolean not null default false,
  updated_at timestamptz not null default now(),
  unique (owner, name)
);
create index if not exists setlists_owner on public.setlists (owner);

create table if not exists public.setlist_items (
  setlist_id uuid not null references public.setlists on delete cascade,
  position int not null,
  song_key text not null,
  checksum text,
  artist text, name text, charter text, album text,
  source jsonb not null default '{}'::jsonb,
  primary key (setlist_id, position)
);

alter table public.setlists enable row level security;
alter table public.setlist_items enable row level security;

drop policy if exists "own setlists" on public.setlists;
create policy "own setlists" on public.setlists for all
  using (owner = auth.uid()) with check (owner = auth.uid());
drop policy if exists "friends read shared setlists" on public.setlists;
create policy "friends read shared setlists" on public.setlists for select
  using (shared and owner <> auth.uid() and public.are_friends(auth.uid(), owner));

drop policy if exists "own setlist items" on public.setlist_items;
create policy "own setlist items" on public.setlist_items for all
  using (exists (select 1 from public.setlists s where s.id = setlist_id and s.owner = auth.uid()))
  with check (exists (select 1 from public.setlists s where s.id = setlist_id and s.owner = auth.uid()));
-- The subquery runs under the setlists policies above, so it only sees lists this user may read.
drop policy if exists "friends read shared setlist items" on public.setlist_items;
create policy "friends read shared setlist items" on public.setlist_items for select
  using (exists (select 1 from public.setlists s
                 where s.id = setlist_id and s.shared and s.owner <> auth.uid()
                   and public.are_friends(auth.uid(), s.owner)));

create or replace function public.setlists_touch() returns trigger
language plpgsql set search_path = '' as $$
begin
  new.updated_at := now();
  return new;
end $$;
drop trigger if exists setlists_touch on public.setlists;
create trigger setlists_touch before update on public.setlists
  for each row execute function public.setlists_touch();

-- Create or replace one of MY setlists and its songs in one atomic call. `shared` is left alone on an
-- existing list (new lists start private). SECURITY INVOKER: RLS applies, so it can only touch my own rows.
create or replace function public.upsert_setlist(p_name text, p_items jsonb) returns uuid
language plpgsql security invoker set search_path = '' as $$
declare
  sid uuid;
begin
  insert into public.setlists (name) values (p_name)
    on conflict (owner, name) do update set updated_at = now()
    returning id into sid;
  delete from public.setlist_items where setlist_id = sid;
  insert into public.setlist_items (setlist_id, position, song_key, checksum, artist, name, charter, album, source)
  select sid, (e.ord - 1)::int, e.item->>'song_key', e.item->>'checksum', e.item->>'artist', e.item->>'name',
         e.item->>'charter', e.item->>'album', coalesce(e.item->'source', '{}'::jsonb)
  from jsonb_array_elements(coalesce(p_items, '[]'::jsonb)) with ordinality as e(item, ord);
  return sid;
end $$;

-- ======================================================================== grants
-- Only signed-in users call the helpers; anon gets nothing.
revoke all on function public.lookup_profile(text) from public, anon;
revoke all on function public.username_available(text) from public, anon;
revoke all on function public.request_friend(text) from public, anon;
revoke all on function public.my_friendships() from public, anon;
revoke all on function public.upsert_setlist(text, jsonb) from public, anon;
revoke all on function public.are_friends(uuid, uuid) from public, anon;
revoke all on function public.favorites_visible(uuid) from public, anon;
grant execute on function public.lookup_profile(text) to authenticated;
grant execute on function public.username_available(text) to authenticated;
grant execute on function public.request_friend(text) to authenticated;
grant execute on function public.my_friendships() to authenticated;
grant execute on function public.upsert_setlist(text, jsonb) to authenticated;
grant execute on function public.are_friends(uuid, uuid) to authenticated;
grant execute on function public.favorites_visible(uuid) to authenticated;

-- ======================================================================== manual RLS checks (run as two real accounts A and B)
-- 1. A: select * from lookup_profile('<B username or code>');           -- one row: username, display_name, avatar_url
-- 2. A: select request_friend('<B code>');  B: select * from my_friendships();  -- B sees an 'incoming' row
-- 3. A (not yet accepted): select * from favorites where user_id = '<B id>';     -- 0 rows
-- 4. B: update friendships set status='accepted' where requester='<A id>';       -- ok; A doing it fails (0 rows)
-- 5. B: insert into share_settings(user_id, favorites_shared) values (auth.uid(), true);
--    A: select count(*) from favorites where user_id = '<B id>';                 -- B's favourites
-- 6. B: update share_settings set favorites_shared=false;  A: same select         -- 0 rows
-- 7. B: select upsert_setlist('Night', '[{"song_key":"x","name":"y"}]');  A: select * from setlists;  -- none
--    B: update setlists set shared=true;  A: select * from setlist_items;                             -- visible
-- 8. C (not a friend): every select above returns 0 rows. A: delete from friendships -> A no longer sees B's data.
