-- Clone Hero Companion 2.6.0: friends can view your profile stats. share_settings.stats_shared = "all my friends can
-- see my play statistics (the Profile numbers, heatmap and song lists)". On by default; off hides everything.
-- Idempotent and safe to run as ONE batch (Management API /database/query). No CONCURRENTLY, no psql meta-commands.
--
--   * A missing share_settings row counts as shared (the default), so existing accounts become visible to their friends
--     without any client action; a user who never wants this switches it off in Friends.
--   * Friends only get SELECT on plays rows. The app asks for explicit columns and never requests `raw`.
--   * Visibility of favourites and setlists (lists_shared, 0003) is untouched.

-- ======================================================================== the column
alter table public.share_settings add column if not exists stats_shared boolean not null default true;

-- ======================================================================== visibility helper
-- May the signed-in user read owner's plays? Accepted friends AND the owner has not switched stats sharing off.
-- Security definer so the friend needs no read access to the owner's share_settings row.
create or replace function public.stats_visible(owner uuid) returns boolean
language sql stable security definer set search_path = public as $$
  select coalesce(public.are_friends(auth.uid(), owner)
                  and coalesce((select s.stats_shared from public.share_settings s where s.user_id = owner), true), false)
$$;

-- ======================================================================== friend SELECT policy (next to "own plays")
drop policy if exists "friends read shared plays" on public.plays;
create policy "friends read shared plays" on public.plays for select
  using (user_id <> auth.uid() and public.stats_visible(user_id));

-- ======================================================================== grants
revoke all on function public.stats_visible(uuid) from public, anon;
grant execute on function public.stats_visible(uuid) to authenticated;

-- ======================================================================== manual RLS checks (two real accounts A and B, accepted friends; C is not a friend)
-- 1. A: select count(*) from plays where user_id = '<B id>';      -- all of B's plays (stats_shared defaults to true)
--    A: select stats_visible('<B id>');                            -- true
-- 2. B: update share_settings set stats_shared = false;  (or insert the row first: upsert into share_settings(user_id, stats_shared))
--    A: the count is 0 and stats_visible('<B id>') is false.
-- 3. B: update share_settings set stats_shared = true;  A sees every play again, also ones B uploads later.
-- 4. C (not a friend): count is 0 and stats_visible('<B id>') is false. A: delete from friendships -> A sees 0 rows.
-- 5. A can never insert/update/delete B's plays (the "own plays" policy is the only write policy).
