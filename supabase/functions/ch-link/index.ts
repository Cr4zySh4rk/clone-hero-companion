// ch-link: link (or unlink) the caller's Companion account to a CH Leaderboards account, verified by the user's own
// CH session. The session token is used for two calls and (normally) a logout, and is never stored or logged.
//   POST {sessionToken}                      -> {accountId, username, profile, alsoLinkedElsewhere}   (CH session logged out)
//   POST {sessionToken, keepSession: true}   -> same, plus sessionKept: true; the CH session is NOT logged out, because
//        the app keeps it in the OS secure storage to launch Clone Hero signed in. If the link fails, it is still logged out.
//   POST {unlink: true, sessionToken?}       -> {unlinked: true}   (the CH session the app kept, if sent, is logged out)
// Deployed with verify_jwt = true. Writes use the service role (the client cannot write the CH columns).
import { adminClient, callerId, CH_API, corsHeaders, fetchChProfile, json } from "../_shared/common.ts";

type Handler = (req: Request) => Promise<Response>;

export const handler: Handler = async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(req) });
  if (req.method !== "POST") return json(req, { error: "method_not_allowed" }, 405);
  const uid = await callerId(req);
  if (!uid) return json(req, { error: "unauthorized" }, 401);

  let body: { sessionToken?: unknown; unlink?: unknown; keepSession?: unknown };
  try { body = await req.json(); } catch { return json(req, { error: "bad_request" }, 400); }
  const db = adminClient();

  if (body.unlink === true) {
    const { error } = await db.from("profiles").update({
      ch_account_id: null, ch_username: null, ch_verified_at: null, ch_profile: null, ch_profile_at: null,
    }).eq("id", uid);
    if (error) return json(req, { error: "db_error" }, 500);
    if (typeof body.sessionToken === "string" && body.sessionToken) await chLogout(body.sessionToken);
    return json(req, { unlinked: true });
  }

  const token = typeof body.sessionToken === "string" ? body.sessionToken : "";
  if (!token) return json(req, { error: "bad_request" }, 400);
  const wantKeep = body.keepSession === true;
  let keep = false;   // true only once the link succeeded; every other path ends the CH session

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
    keep = wantKeep;
    return json(req, { accountId, username, profile, alsoLinkedElsewhere: (count ?? 0) > 0, sessionKept: keep });
  } catch (_e) {
    return json(req, { error: "ch_unreachable" }, 502);
  } finally {
    // End the CH session unless the app asked to keep it and the link succeeded; failures here do not matter.
    if (!keep) await chLogout(token);
  }
};

async function chLogout(token: string): Promise<void> {
  try {
    await fetch(`${CH_API}/v2/auth/logout`, { method: "POST", headers: { Authorization: `Bearer ${token}` } });
  } catch (_e) { /* ignore */ }
}

if (import.meta.main) Deno.serve(handler);
