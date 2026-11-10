-- Clone Hero Companion 2.6.4: a career snapshot on the profile row, so the website (career.html) can show the numbers
-- the app computes (scoredata.bin star/FC counts, CH Leaderboards account stats and ranks). Browsers cannot call
-- api.clonehero.net (no CORS), so the app uploads them on each cloud sync, and only when they changed.
-- Idempotent and safe to run as ONE batch. No CONCURRENTLY, no psql meta-commands.
--
--   * career     jsonb        {v, app, ch_username, totals{...}, stats{...}, ranks{...}}
--   * career_at  timestamptz  when the app last uploaded it
--   * The existing "own profile" policy (0001, for all, id = auth.uid()) already covers both columns: a user reads and
--     writes only their own row. Friends cannot read profiles rows (only the lookup functions of 0002 return a
--     username, display name and avatar), so the snapshot is private to its owner.

alter table public.profiles add column if not exists career jsonb;
alter table public.profiles add column if not exists career_at timestamptz;
