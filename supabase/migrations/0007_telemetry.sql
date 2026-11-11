-- Clone Hero Companion 2.8.0: anonymous usage statistics (opt-out in the app, honours DNT/GPC on the website) and an
-- admin-only dashboard. See PRIVACY.md for exactly what is collected.
-- Idempotent and safe to run as ONE batch. No CONCURRENTLY, no psql meta-commands.
--
--   * telemetry_events   raw events. RLS on, NO policies: clients can neither read nor write it directly.
--   * admins             who may read the dashboard. RLS on, NO policies.
--   * track(events)      the only way in: security definer, anon + authenticated. Validates every event against a fixed
--                        allow-list, ignores client timestamps, drops (never errors on) an install/session that sends
--                        more than 300 events in an hour. No IP address is stored (the function never looks at one).
--   * admin_stats(days)  admin-only aggregate JSON for site/admin.html.
--   * purge_telemetry()  admin-only; raw events are kept 180 days (scheduled with pg_cron when that extension is on).

-- ---------------------------------------------------------------------------- 1. tables
create table if not exists public.telemetry_events (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  source      text not null check (source in ('app', 'web')),
  install_id  uuid,
  session_id  uuid,
  name        text not null,
  props       jsonb not null default '{}'::jsonb,
  app_version text,
  os          text,
  locale      text
);
create index if not exists telemetry_events_at on public.telemetry_events (at);
create index if not exists telemetry_events_name_at on public.telemetry_events (name, at);
create index if not exists telemetry_events_install_at on public.telemetry_events (install_id, at);
create index if not exists telemetry_events_session_at on public.telemetry_events (session_id, at);
alter table public.telemetry_events enable row level security;
revoke all on public.telemetry_events from anon, authenticated;
revoke all on sequence public.telemetry_events_id_seq from anon, authenticated;

create table if not exists public.admins (
  user_id uuid primary key references auth.users on delete cascade
);
alter table public.admins enable row level security;
revoke all on public.admins from anon, authenticated;

-- the one admin (this id is deliberately not in any client code; the dashboard asks the server)
insert into public.admins (user_id)
  select id from auth.users where id = 'ba706858-0ead-45ed-bd1a-874d78984c7c'
  on conflict do nothing;

-- ---------------------------------------------------------------------------- 2. is_admin()
create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public, pg_temp as $$
  select exists (select 1 from public.admins where user_id = auth.uid())
$$;
revoke all on function public.is_admin() from public, anon;
grant execute on function public.is_admin() to authenticated;

-- ---------------------------------------------------------------------------- 3. track(events)
-- events: [{source:'app'|'web', install_id?, session_id?, name, props?, app_version?, os?, locale?}, ...] (1 to 50).
-- app events need install_id; web events need session_id. Keep the allow-list in step with chcompanion/telemetry.py
-- and site/t.js.
create or replace function public.track(events jsonb) returns void
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  -- event name -> source and the property keys it may carry
  allowed constant jsonb := '{
    "app_start":           {"s": "app", "k": ["arch", "signed_in", "library_size_bucket", "gpu_kind"]},
    "heartbeat":           {"s": "app", "k": ["view", "focused"]},
    "app_quit":            {"s": "app", "k": ["session_s"]},
    "view":                {"s": "app", "k": ["name"]},
    "download_chart":      {"s": "app", "k": ["source"]},
    "add_video":           {"s": "app", "k": ["result"]},
    "add_lyrics":          {"s": "app", "k": ["result"]},
    "convert_videos":      {"s": "app", "k": ["count"]},
    "spotify_import":      {"s": "app", "k": []},
    "setlist_create":      {"s": "app", "k": []},
    "favourite":           {"s": "app", "k": ["on"]},
    "share_stats":         {"s": "app", "k": ["target"]},
    "friend_request":      {"s": "app", "k": []},
    "friend_view_profile": {"s": "app", "k": []},
    "ch_link":             {"s": "app", "k": ["result"]},
    "update_install":      {"s": "app", "k": ["result"]},
    "job_cancel":          {"s": "app", "k": []},
    "launch_game":         {"s": "app", "k": []},
    "pageview":            {"s": "web", "k": ["path", "ref_domain", "w_bucket"]},
    "download_click":      {"s": "web", "k": ["os"]},
    "career_signin":       {"s": "web", "k": ["method"]}
  }';
  uuid_re constant text := '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$';
  tag_re  constant text := '^[A-Za-z0-9._+-]{1,20}$';
  ev jsonb; spec jsonb; v_name text; v_source text; v_props jsonb; v_install uuid; v_session uuid;
  who text; seen jsonb := '{}'::jsonb; n int; pk text; pv jsonb; good boolean;
begin
  if events is null or jsonb_typeof(events) <> 'array' or jsonb_array_length(events) not between 1 and 50 then
    raise exception 'events must be an array of 1 to 50 items' using errcode = '22023';
  end if;
  for ev in select value from jsonb_array_elements(events) loop
    continue when jsonb_typeof(ev) <> 'object';
    v_name := ev->>'name';
    v_source := ev->>'source';
    spec := allowed -> v_name;
    continue when spec is null or v_source not in ('app', 'web') or spec->>'s' <> v_source;
    -- ids
    continue when ev->>'install_id' is not null and (jsonb_typeof(ev->'install_id') <> 'string' or ev->>'install_id' !~ uuid_re);
    continue when ev->>'session_id' is not null and (jsonb_typeof(ev->'session_id') <> 'string' or ev->>'session_id' !~ uuid_re);
    v_install := nullif(ev->>'install_id', '')::uuid;
    v_session := nullif(ev->>'session_id', '')::uuid;
    continue when v_source = 'app' and v_install is null;
    continue when v_source = 'web' and v_session is null;
    if v_source = 'web' then v_install := null; end if;
    -- props: an object, at most 8 keys, only this event's keys, values string (<= 40 chars) / number / bool
    v_props := coalesce(ev->'props', '{}'::jsonb);
    continue when jsonb_typeof(v_props) <> 'object' or (select count(*) from jsonb_object_keys(v_props)) > 8;
    good := true;
    for pk, pv in select key, value from jsonb_each(v_props) loop
      if not (spec->'k' ? pk)
         or jsonb_typeof(pv) not in ('string', 'number', 'boolean')
         or (jsonb_typeof(pv) = 'string' and char_length(pv #>> '{}') > 40)
         or (jsonb_typeof(pv) = 'number' and abs((pv #>> '{}')::numeric) > 100000000) then
        good := false; exit;
      end if;
    end loop;
    continue when not good;
    -- cheap abuse limit: more than 300 events in the last hour from this install/session -> drop quietly
    who := coalesce(v_install, v_session)::text;
    if not seen ? who then
      select count(*) into n from public.telemetry_events
        where at > now() - interval '1 hour'
          and case when v_install is not null then install_id = v_install else session_id = v_session end;
      seen := seen || jsonb_build_object(who, n);
    end if;
    continue when (seen ->> who)::int >= 300;
    seen := seen || jsonb_build_object(who, (seen ->> who)::int + 1);
    insert into public.telemetry_events (source, install_id, session_id, name, props, app_version, os, locale)
    values (v_source, v_install, v_session, v_name, v_props,
            case when ev->>'app_version' ~ tag_re then ev->>'app_version' end,
            case when ev->>'os' ~ tag_re then ev->>'os' end,
            case when ev->>'locale' ~ tag_re then ev->>'locale' end);
  end loop;
end
$$;
revoke all on function public.track(jsonb) from public;
grant execute on function public.track(jsonb) to anon, authenticated;

-- ---------------------------------------------------------------------------- 4. purge_telemetry()
create or replace function public.purge_telemetry(keep_days int default 180) returns int
language plpgsql security definer set search_path = public, pg_temp as $$
declare n int;
begin
  if not public.is_admin() then
    raise exception 'not authorised' using errcode = '42501';
  end if;
  delete from public.telemetry_events where at < now() - make_interval(days => greatest(coalesce(keep_days, 180), 1));
  get diagnostics n = row_count;
  return n;
end
$$;
revoke all on function public.purge_telemetry(int) from public, anon;
grant execute on function public.purge_telemetry(int) to authenticated;

-- nightly purge of events older than 180 days, when the pg_cron extension is enabled (otherwise use the dashboard button)
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule('purge-telemetry', '17 3 * * *',
      $job$delete from public.telemetry_events where at < now() - interval '180 days'$job$);
  end if;
exception when others then
  raise notice 'pg_cron schedule skipped: %', sqlerrm;
end
$$;

-- ---------------------------------------------------------------------------- 5. admin_stats(days)
create or replace function public.admin_stats(days int default 30) returns jsonb
language plpgsql stable security definer set search_path = public, pg_temp as $$
declare
  d int := least(greatest(coalesce(days, 30), 1), 365);
  today date := (now() at time zone 'utc')::date;
  first_day date;
  since timestamptz;
  app jsonb; web jsonb; acc jsonb;
  v_new jsonb; v_active jsonb; v_sessions jsonb; v_versions jsonb; v_os jsonb; v_locales jsonb; v_top jsonb; v_feat jsonb;
  v_len jsonb; v_wvis jsonb; v_pages jsonb; v_refs jsonb; v_dl jsonb; v_signups jsonb; v_views jsonb;
begin
  if not public.is_admin() then
    raise exception 'not authorised' using errcode = '42501';
  end if;
  first_day := today - (d - 1);
  since := first_day::timestamp at time zone 'utc';

  -- series over every day of the range (zeros included)
  select coalesce(jsonb_agg(jsonb_build_object('day', g.day, 'n', coalesce(c.n, 0)) order by g.day), '[]') into v_new
    from generate_series(first_day, today, interval '1 day') as g(day)
    left join (select f.first_day as day, count(*) as n
                 from (select install_id, (min(at) at time zone 'utc')::date as first_day
                         from public.telemetry_events where source = 'app' group by install_id) f
                group by 1) c on c.day = g.day::date;

  select coalesce(jsonb_agg(jsonb_build_object('day', g.day, 'n', coalesce(c.n, 0)) order by g.day), '[]') into v_active
    from generate_series(first_day, today, interval '1 day') as g(day)
    left join (select (at at time zone 'utc')::date as day, count(distinct install_id) as n
                 from public.telemetry_events where source = 'app' and at >= since group by 1) c on c.day = g.day::date;

  select coalesce(jsonb_agg(jsonb_build_object('day', g.day, 'n', coalesce(c.n, 0)) order by g.day), '[]') into v_sessions
    from generate_series(first_day, today, interval '1 day') as g(day)
    left join (select (at at time zone 'utc')::date as day, count(distinct session_id) as n
                 from public.telemetry_events where source = 'app' and at >= since group by 1) c on c.day = g.day::date;

  -- session length: the app_quit figure when there is one (a crash leaves none), else 5 minutes per heartbeat
  with s as (
    select session_id,
           coalesce(max((props ->> 'session_s')::numeric) filter (where name = 'app_quit'),
                    count(*) filter (where name = 'heartbeat') * 300) as secs
      from public.telemetry_events
     where source = 'app' and at >= since and session_id is not null
     group by session_id)
  select jsonb_build_object('sessions', count(*), 'avg_s', coalesce(round(avg(secs)), 0),
                            'median_s', coalesce(round((percentile_cont(0.5) within group (order by secs))::numeric), 0),
                            'total_hours', coalesce(round(sum(secs) / 3600.0, 1), 0))
    into v_len from s where secs > 0;

  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n) order by n desc), '[]') into v_versions
    from (select coalesce(nullif(app_version, ''), 'unknown') as k, count(distinct install_id) as n
            from public.telemetry_events where source = 'app' and at >= since group by 1 order by 2 desc limit 20) x;
  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n) order by n desc), '[]') into v_os
    from (select coalesce(nullif(os, ''), 'unknown') as k, count(distinct install_id) as n
            from public.telemetry_events where source = 'app' and at >= since group by 1 order by 2 desc limit 20) x;
  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n) order by n desc), '[]') into v_locales
    from (select coalesce(nullif(locale, ''), 'unknown') as k, count(distinct install_id) as n
            from public.telemetry_events where source = 'app' and at >= since group by 1 order by 2 desc limit 20) x;

  select coalesce(jsonb_agg(jsonb_build_object('name', name, 'events', n, 'installs', i) order by n desc), '[]') into v_top
    from (select name, count(*) as n, count(distinct install_id) as i
            from public.telemetry_events where source = 'app' and at >= since group by name order by 2 desc limit 30) x;

  select coalesce(jsonb_agg(jsonb_build_object('name', f.name, 'events', coalesce(c.n, 0), 'installs', coalesce(c.i, 0)) order by f.ord), '[]') into v_feat
    from unnest(array['download_chart', 'add_video', 'add_lyrics', 'convert_videos', 'spotify_import', 'setlist_create', 'favourite',
                      'share_stats', 'friend_request', 'friend_view_profile', 'ch_link', 'update_install', 'job_cancel', 'launch_game'])
         with ordinality as f(name, ord)
    left join (select name, count(*) as n, count(distinct install_id) as i
                 from public.telemetry_events where source = 'app' and at >= since group by name) c on c.name = f.name;

  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n) order by n desc), '[]') into v_views
    from (select props ->> 'name' as k, count(*) as n from public.telemetry_events
           where source = 'app' and name = 'view' and at >= since group by 1 order by 2 desc limit 20) x;

  app := jsonb_build_object(
    'dau', (select count(distinct install_id) from public.telemetry_events where source = 'app' and at >= now() - interval '1 day'),
    'wau', (select count(distinct install_id) from public.telemetry_events where source = 'app' and at >= now() - interval '7 days'),
    'mau', (select count(distinct install_id) from public.telemetry_events where source = 'app' and at >= now() - interval '30 days'),
    'installs_all', (select count(distinct install_id) from public.telemetry_events where source = 'app'),
    'installs_range', (select count(distinct install_id) from public.telemetry_events where source = 'app' and at >= since),
    'new_installs_range', (select coalesce(sum((e ->> 'n')::int), 0) from jsonb_array_elements(v_new) e),
    'new_installs', v_new, 'active_installs', v_active, 'sessions_per_day', v_sessions, 'session', v_len,
    'versions', v_versions, 'os', v_os, 'locales', v_locales, 'top_events', v_top, 'features', v_feat, 'views', v_views);

  select coalesce(jsonb_agg(jsonb_build_object('day', g.day, 'visitors', coalesce(c.v, 0), 'views', coalesce(c.p, 0)) order by g.day), '[]') into v_wvis
    from generate_series(first_day, today, interval '1 day') as g(day)
    left join (select (at at time zone 'utc')::date as day, count(distinct session_id) as v, count(*) as p
                 from public.telemetry_events where source = 'web' and name = 'pageview' and at >= since group by 1) c on c.day = g.day::date;
  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n, 'visitors', v) order by n desc), '[]') into v_pages
    from (select coalesce(nullif(props ->> 'path', ''), '/') as k, count(*) as n, count(distinct session_id) as v
            from public.telemetry_events where source = 'web' and name = 'pageview' and at >= since group by 1 order by 2 desc limit 15) x;
  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n) order by n desc), '[]') into v_refs
    from (select props ->> 'ref_domain' as k, count(distinct session_id) as n
            from public.telemetry_events
           where source = 'web' and name = 'pageview' and at >= since and coalesce(props ->> 'ref_domain', '') <> '' group by 1 order by 2 desc limit 15) x;
  select coalesce(jsonb_agg(jsonb_build_object('k', k, 'n', n) order by n desc), '[]') into v_dl
    from (select coalesce(nullif(props ->> 'os', ''), 'unknown') as k, count(*) as n
            from public.telemetry_events where source = 'web' and name = 'download_click' and at >= since group by 1 order by 2 desc) x;
  web := jsonb_build_object(
    'visitors_range', (select count(distinct session_id) from public.telemetry_events where source = 'web' and name = 'pageview' and at >= since),
    'pageviews_range', (select count(*) from public.telemetry_events where source = 'web' and name = 'pageview' and at >= since),
    'daily', v_wvis, 'top_pages', v_pages, 'referrers', v_refs, 'download_clicks', v_dl,
    'career_signins', (select count(*) from public.telemetry_events where source = 'web' and name = 'career_signin' and at >= since));

  select coalesce(jsonb_agg(jsonb_build_object('day', g.day, 'n', coalesce(c.n, 0)) order by g.day), '[]') into v_signups
    from generate_series(first_day, today, interval '1 day') as g(day)
    left join (select (created_at at time zone 'utc')::date as day, count(*) as n
                 from auth.users where created_at >= since group by 1) c on c.day = g.day::date;
  acc := jsonb_build_object(
    'total_users', (select count(*) from auth.users),
    'signed_up_range', (select count(*) from auth.users where created_at >= since),
    'signups', v_signups,
    'verified_ch_links', (select count(*) from public.profiles where ch_verified_at is not null),
    'users_with_friends', (select count(*) from (select requester as u from public.friendships where status = 'accepted'
                                                  union select addressee from public.friendships where status = 'accepted') f),
    'installs_signed_in', (select count(distinct install_id) from public.telemetry_events
                            where source = 'app' and name = 'app_start' and at >= since and props ->> 'signed_in' = 'true'),
    'installs_started', (select count(distinct install_id) from public.telemetry_events
                          where source = 'app' and name = 'app_start' and at >= since));

  return jsonb_build_object('days', d, 'generated_at', now(), 'app', app, 'web', web, 'accounts', acc);
end
$$;
revoke all on function public.admin_stats(int) from public, anon;
grant execute on function public.admin_stats(int) to authenticated;

-- ---------------------------------------------------------------------------- 6. SQL tests (run by hand in the SQL editor; they roll back)
-- begin;
--   -- a valid batch is stored, an unknown event / prop / bad uuid is dropped, oversize batches raise
--   select public.track('[{"source":"app","install_id":"11111111-1111-4111-8111-111111111111","name":"view","props":{"name":"library"},"app_version":"2.8.0","os":"linux","locale":"en"}]');
--   select public.track('[{"source":"app","install_id":"11111111-1111-4111-8111-111111111111","name":"song_title","props":{}}]');          -- unknown name: dropped
--   select public.track('[{"source":"app","install_id":"11111111-1111-4111-8111-111111111111","name":"view","props":{"title":"x"}}]');      -- key not allowed: dropped
--   select public.track('[{"source":"app","install_id":"not-a-uuid","name":"view","props":{}}]');                                           -- bad uuid: dropped
--   select public.track('[{"source":"web","install_id":"11111111-1111-4111-8111-111111111111","name":"view","props":{}}]');                 -- wrong source for the event: dropped
--   select count(*) = 1 as only_the_valid_one_stored from public.telemetry_events where install_id = '11111111-1111-4111-8111-111111111111';
--   select public.track('[]');                                                                                                              -- raises 22023
-- rollback;
-- begin;
--   -- not an admin (anon, or any signed-in user not in public.admins): both raise 42501 'not authorised'
--   set local role authenticated; select set_config('request.jwt.claims', '{"sub":"00000000-0000-4000-8000-000000000000","role":"authenticated"}', true);
--   select public.admin_stats(30);        -- must raise
--   select public.purge_telemetry(180);   -- must raise
--   select * from public.telemetry_events; -- permission denied
-- rollback;
