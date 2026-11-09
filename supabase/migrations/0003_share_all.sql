-- Clone Hero Companion 2.4.0: ONE sharing switch. share_settings.lists_shared = "all my friends can see my
-- favourites and ALL my setlists (including ones created later)". Off by default, off hides everything.
-- Idempotent and safe to run as ONE batch (Management API /database/query). No CONCURRENTLY, no psql meta-commands.
--
-- Backward compatibility with 2.3.x clients (which write share_settings.favorites_shared and setlists.shared):
--   * The old columns stay. Nothing in RLS reads setlists.shared or favorites_shared any more: visibility depends only
--     on lists_shared (via lists_visible / favorites_visible). A 2.3.x per-setlist toggle therefore has NO effect.
--   * A trigger keeps favorites_shared and lists_shared in step: a 2.3.x client switching favorites_shared on (or off)
--     switches lists_shared on (or off), so an older app can still turn sharing on, and its favourites checkbox shows
--     the real state. A 2.4 client only writes lists_shared; the trigger mirrors it into favorites_shared. If one
--     statement changes both columns, lists_shared wins.
--   * 2.3.x clients only upload setlists the user shared; their lists reach friends once 2.4 uploads them (2.4 syncs
--     every local setlist while the switch is on). Dropping the old columns is left to a later migration.

-- ======================================================================== the column + one-time data migration
-- The backfill runs only when the column is first added, so re-running this file never switches sharing back on.
do $$
begin
  if not exists (select 1 from information_schema.columns
                 where table_schema = 'public' and table_name = 'share_settings' and column_name = 'lists_shared') then
    alter table public.share_settings add column lists_shared boolean not null default false;
    -- lists_shared = favorites_shared OR (any setlist shared), per user (creating the row when only setlists were shared)
    insert into public.share_settings (user_id, lists_shared)
    select u.user_id, true
    from (select s.user_id from public.share_settings s where s.favorites_shared
          union
          select l.owner from public.setlists l where l.shared) u
    on conflict (user_id) do update set lists_shared = true;
  end if;
end $$;

-- ======================================================================== visibility helpers
-- May the signed-in user read owner's favourites and setlists? Accepted friends AND the owner switched sharing on.
-- Security definer so the friend needs no read access to the owner's share_settings row. Also callable as an RPC:
-- the app asks it first so it can tell "not sharing" from "shares nothing yet".
create or replace function public.lists_visible(owner uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select coalesce(public.are_friends(auth.uid(), owner)
                  and coalesce((select s.lists_shared from public.share_settings s where s.user_id = owner), false), false)
$$;

-- Kept (same signature) for 2.3.x callers; now driven by lists_shared instead of favorites_shared.
create or replace function public.favorites_visible(owner uuid) returns boolean
language sql stable security definer set search_path = '' as $$
  select owner = auth.uid() or public.lists_visible(owner)
$$;

-- ======================================================================== old clients: keep the two switches in step
create or replace function public.share_settings_sync_flags() returns trigger
language plpgsql set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    if new.lists_shared then new.favorites_shared := true;
    elsif new.favorites_shared then new.lists_shared := true;
    end if;
  elsif new.lists_shared is distinct from old.lists_shared then
    new.favorites_shared := new.lists_shared;        -- a 2.4 client (or both columns at once): lists_shared wins
  elsif new.favorites_shared is distinct from old.favorites_shared then
    new.lists_shared := new.favorites_shared;        -- a 2.3.x client's favourites checkbox
  end if;
  return new;
end $$;
drop trigger if exists share_settings_sync_flags on public.share_settings;
create trigger share_settings_sync_flags before insert or update on public.share_settings
  for each row execute function public.share_settings_sync_flags();

-- ======================================================================== friend SELECT policies -> lists_visible
drop policy if exists "friends read shared favorites" on public.favorites;
create policy "friends read shared favorites" on public.favorites for select
  using (user_id <> auth.uid() and public.lists_visible(user_id));

drop policy if exists "friends read shared setlists" on public.setlists;
create policy "friends read shared setlists" on public.setlists for select
  using (owner <> auth.uid() and public.lists_visible(owner));

-- The subquery runs under the setlists policies, so it only sees lists this user may read; stated again for clarity.
drop policy if exists "friends read shared setlist items" on public.setlist_items;
create policy "friends read shared setlist items" on public.setlist_items for select
  using (exists (select 1 from public.setlists s
                 where s.id = setlist_id and s.owner <> auth.uid() and public.lists_visible(s.owner)));

-- ======================================================================== grants
revoke all on function public.lists_visible(uuid) from public, anon;
revoke all on function public.favorites_visible(uuid) from public, anon;
grant execute on function public.lists_visible(uuid) to authenticated;
grant execute on function public.favorites_visible(uuid) to authenticated;

-- ======================================================================== manual RLS checks (two real accounts A and B, accepted friends; C is not a friend)
-- 1. B: upsert into share_settings(user_id, lists_shared) values (auth.uid(), false);  B has favourites and 2 setlists.
--    A: select count(*) from favorites where user_id = '<B id>';  select count(*) from setlists where owner = '<B id>';  -- 0 and 0
--    A: select lists_visible('<B id>');                                                                                   -- false
-- 2. B: update share_settings set lists_shared = true;   (favorites_shared flips to true by the trigger)
--    A: the two counts now return all of B's favourites and ALL setlists (also a setlist B creates afterwards);
--    A: select count(*) from setlist_items where setlist_id in (select id from setlists where owner = '<B id>');         -- all items
--    A: select lists_visible('<B id>');                                                                                   -- true
-- 3. B: update setlists set shared = false;  A: counts unchanged (the old per-list flag has no effect).
-- 4. B: update share_settings set lists_shared = false;  A: counts are 0 again; setlist_items 0 too.
-- 5. 2.3.x compat: B: update share_settings set favorites_shared = true;  select lists_shared from share_settings;  -- true; A sees lists
--    B: update share_settings set favorites_shared = false;                                                          -- lists_shared false
-- 6. Migration of old data (before running this file): B had favorites_shared=false but one setlist shared=true ->
--    afterwards B has a share_settings row with lists_shared = true. Re-run the file after B switched off: still false.
-- 7. C (not a friend): every select above returns 0 rows and lists_visible('<B id>') is false. A: delete from friendships
--    -> A no longer sees B's data. A can never insert/update/delete B's rows or B's share_settings.
