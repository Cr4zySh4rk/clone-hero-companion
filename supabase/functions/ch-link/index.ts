// ch-link: link (or unlink) the caller's Companion account to a CH Leaderboards account, verified by the user's own
// CH session. The session token is used for two calls and a logout, and is never stored or logged.
//   POST {sessionToken}  -> {accountId, username, profile, alsoLinkedElsewhere}
//   POST {unlink: true}  -> {unlinked: true}
// Deployed with verify_jwt = true. Writes use the service role (the client cannot write the CH columns).
import { adminClient, callerId, CH_API, corsHeaders, fetchChProfile, json } from "../_shared/common.ts";

type Handler = (req: Request) => Promise<Response>;

export const handler: Handler = async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(req) });
  if (req.method !== "POST") return json(req, { error: "method_not_allowed" }, 405);
  const uid = await callerId(req);
  if (!uid) return json(req, { error: "unauthorized" }, 401);

  let body: { sessionToken?: unknown; unlink?: unknown };
  try { body = await req.json(); } catch { return json(req, { error: "bad_request" }, 400); }
  const db = adminClient();

  if (body.unlink === true) {
    const { error } = await db.from("profiles").update({
      ch_account_id: null, ch_username: null, ch_verified_at: null, ch_profile: null, ch_profile_at: null,
    }).eq("id", uid);
    if (error) return json(req, { error: "db_error" }, 500);
    return json(req, { unlinked: true });
  }

  const token = typeof body.sessionToken === "string" ? body.sessionToken : "";
  if (!token) return json(req, { error: "bad_request" }, 400);

  try {
    const me = await fetch(`${CH_API}/v1/account/@me`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });
    if (!me.ok) return json(req, { error: "ch_session_invalid" }, 401);
    const acct = await me.json();
    const accountId = Number(acct?.accountId ?? acct?.id ?? acct?.account?.accountId);
    if (!Number.isSafeInteger(accountId) || accountId <= 0) return json(req, { error: "ch_session_invalid" }, 401);

    const profile = await fetchChProfile(accountId);
    const username = String(profile?.username ?? acct?.username ?? acct?.account?.username ?? "");
    if (!username) return json(req, { error: "ch_profile_unavailable" }, 502);
    const now = new Date().toISOString();
    const { error } = await db.from("profiles").upsert({
      id: uid, ch_account_id: accountId, ch_username: username, ch_verified_at: now,
      ch_profile: profile, ch_profile_at: profile ? now : null,
    }, { onConflict: "id" });
    if (error) return json(req, { error: "db_error" }, 500);

    // Nothing says a CH account may be linked to only one Companion user, so allow it and tell the app.
    const { count } = await db.from("profiles").select("id", { count: "exact", head: true })
      .eq("ch_account_id", accountId).neq("id", uid).not("ch_verified_at", "is", null);
    return json(req, { accountId, username, profile, alsoLinkedElsewhere: (count ?? 0) > 0 });
  } catch (_e) {
    return json(req, { error: "ch_unreachable" }, 502);
  } finally {
    // Always end the CH session; failures here do not matter.
    try {
      await fetch(`${CH_API}/v2/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
    } catch (_e) { /* ignore */ }
  }
};

if (import.meta.main) Deno.serve(handler);
