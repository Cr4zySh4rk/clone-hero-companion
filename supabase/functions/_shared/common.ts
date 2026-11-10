// Shared helpers for the ch-link and ch-profile Edge Functions (Deno).
import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2.45.4";

export const CH_API = "https://api.clonehero.net";
export const ALLOWED_ORIGINS = [
  "https://cloneherocompanion.com",
  "https://www.cloneherocompanion.com",
  "http://127.0.0.1:47823",
];

export function corsHeaders(req: Request): Record<string, string> {
  const origin = req.headers.get("Origin") ?? "";
  const h: Record<string, string> = {
    "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-client-info",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin",
  };
  if (ALLOWED_ORIGINS.includes(origin)) h["Access-Control-Allow-Origin"] = origin;
  return h;
}

export function json(req: Request, body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(req), "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

/** The caller's user id, from their JWT (verify_jwt is on; this also checks the user still exists). */
export async function callerId(req: Request): Promise<string | null> {
  const auth = req.headers.get("Authorization") ?? "";
  if (!auth) return null;
  const client = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_ANON_KEY")!, {
    global: { headers: { Authorization: auth } },
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data, error } = await client.auth.getUser();
  if (error || !data?.user) return null;
  return data.user.id;
}

export function adminClient(): SupabaseClient {
  return createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

/** The public CH profile of an account, or null when CH does not answer 200. */
export async function fetchChProfile(accountId: number): Promise<Record<string, unknown> | null> {
  const r = await fetch(`${CH_API}/v1/account/${accountId}/profile`, { headers: { Accept: "application/json" } });
  if (!r.ok) return null;
  const d = await r.json();
  return d && typeof d === "object" ? d : null;
}
