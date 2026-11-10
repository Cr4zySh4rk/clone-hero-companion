// ch-profile: the caller's verified CH Leaderboards profile, at most one live CH fetch per 60 seconds.
//   POST {} -> {profile, at}      404 {error:"not_linked"} when there is no verified link.
// Deployed with verify_jwt = true. Reads and writes with the service role.
import { adminClient, callerId, corsHeaders, fetchChProfile, json } from "../_shared/common.ts";

const MAX_AGE_MS = 60_000;

export const handler = async (req: Request): Promise<Response> => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(req) });
  if (req.method !== "POST") return json(req, { error: "method_not_allowed" }, 405);
  const uid = await callerId(req);
  if (!uid) return json(req, { error: "unauthorized" }, 401);

  const db = adminClient();
  const { data: row, error } = await db.from("profiles")
    .select("ch_account_id, ch_verified_at, ch_profile, ch_profile_at").eq("id", uid).maybeSingle();
  if (error) return json(req, { error: "db_error" }, 500);
  if (!row || !row.ch_verified_at || !row.ch_account_id) return json(req, { error: "not_linked" }, 404);

  const age = row.ch_profile_at ? Date.now() - new Date(row.ch_profile_at).getTime() : Infinity;
  if (row.ch_profile && age <= MAX_AGE_MS) return json(req, { profile: row.ch_profile, at: row.ch_profile_at });

  const profile = await fetchChProfile(Number(row.ch_account_id)).catch(() => null);
  if (!profile) {
    // CH is down: serve the last stored copy rather than nothing.
    if (row.ch_profile) return json(req, { profile: row.ch_profile, at: row.ch_profile_at, stale: true });
    return json(req, { error: "ch_unreachable" }, 502);
  }
  const at = new Date().toISOString();
  await db.from("profiles").update({ ch_profile: profile, ch_profile_at: at }).eq("id", uid);
  return json(req, { profile, at });
};

if (import.meta.main) Deno.serve(handler);
