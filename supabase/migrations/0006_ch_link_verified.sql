-- Clone Hero Companion 2.7.0: CH Leaderboards account linking VERIFIED by a CH sign-in, enforced on the server.
-- Until now profiles.ch_username was typed by the user, so anyone could claim any CH account. From now on the CH
-- columns are written only by the `ch-link` / `ch-profile` Edge Functions (service role), which prove ownership by
-- calling api.clonehero.net with the user's own CH session. Clients can read them (own row, policy "own profile")
-- but can never write them.
-- Idempotent and safe to run as ONE batch. No CONCURRENTLY, no psql meta-commands.
--
--   * ch_username    text         (already exists, 0001) the verified CH username; set by ch-link only
--   * ch_account_id  bigint       the verified CH account id
--   * ch_verified_at timestamptz  null = NOT verified (every pre-2.7.0 row stays null: its ch_username is unverified)
--   * ch_profile     jsonb        the raw /v1/account/{id}/profile response, written by the Edge Functions
--   * ch_profile_at  timestamptz  when ch_profile was fetched

alter table public.profiles add column if not exists ch_account_id bigint;
alter table public.profiles add column if not exists ch_verified_at timestamptz;
alter table public.profiles add column if not exists ch_profile jsonb;
alter table public.profiles add column if not exists ch_profile_at timestamptz;

-- ---------------------------------------------------------------------------- 1. column privileges (UPDATE)
-- Revoke table-wide UPDATE, then grant it back for every column EXCEPT the CH ones. Columns come from
-- 0001 (id, display_name, avatar_url, created_at, updated_at), 0002 (username, friend_code), 0005 (career, career_at).
revoke update on public.profiles from authenticated, anon;
grant update (id, display_name, avatar_url, created_at, updated_at, username, friend_code, career, career_at)
  on public.profiles to authenticated;

-- ---------------------------------------------------------------------------- 2. guard trigger (INSERT and UPDATE)
-- Why a trigger as well as the grants: the "own profile" policy is `for all`, so a client may also INSERT or upsert its
-- own row, and a later `grant all` / new default privileges (Supabase re-grants on new objects) could silently undo the
-- column grants. A BEFORE INSERT OR UPDATE trigger holds whatever the grants do: for the API roles (authenticated, anon)
-- it nulls the CH columns on insert and keeps the old values on update. It is NOT security definer, so current_user is
-- the role PostgREST switched to: the service role (Edge Functions), the SQL editor and migrations are unaffected.
create or replace function public.profiles_ch_guard() returns trigger
language plpgsql set search_path = '' as $$
begin
  if current_user in ('authenticated', 'anon') then
    if tg_op = 'INSERT' then
      new.ch_username := null; new.ch_account_id := null; new.ch_verified_at := null;
      new.ch_profile := null;  new.ch_profile_at := null;
    else
      new.ch_username := old.ch_username; new.ch_account_id := old.ch_account_id;
      new.ch_verified_at := old.ch_verified_at;
      new.ch_profile := old.ch_profile; new.ch_profile_at := old.ch_profile_at;
    end if;
  end if;
  return new;
end $$;
drop trigger if exists profiles_ch_guard on public.profiles;
create trigger profiles_ch_guard before insert or update on public.profiles
  for each row execute function public.profiles_ch_guard();

-- ---------------------------------------------------------------------------- 3. manual RLS check (run as comments, by hand)
-- In the SQL editor, impersonate a normal signed-in user, then try to write each CH column; every statement must leave
-- the CH columns unchanged (the UPDATE grants raise "permission denied for table profiles"; the trigger covers insert):
--
--   begin;
--   set local role authenticated;
--   select set_config('request.jwt.claims', '{"sub":"<some user uuid>","role":"authenticated"}', true);
--   update public.profiles set ch_username = 'someone' where id = '<some user uuid>';       -- expect: permission denied
--   update public.profiles set ch_verified_at = now() where id = '<some user uuid>';        -- expect: permission denied
--   update public.profiles set ch_account_id = 1, ch_profile = '{}' where id = '<some user uuid>'; -- expect: permission denied
--   update public.profiles set display_name = display_name where id = '<some user uuid>';   -- expect: UPDATE 1 (allowed)
--   rollback;
--
-- Trigger check (as the table owner, simulating an API role writing past the grants):
--   begin;
--   grant update on public.profiles to authenticated;   -- temporarily undo the column grants
--   set local role authenticated;
--   select set_config('request.jwt.claims', '{"sub":"<some user uuid>","role":"authenticated"}', true);
--   update public.profiles set ch_username = 'x', ch_verified_at = now() where id = '<some user uuid>';
--   select ch_username, ch_verified_at from public.profiles where id = '<some user uuid>';  -- expect: the OLD values
--   rollback;
