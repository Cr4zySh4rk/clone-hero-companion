// Run: deno test --allow-env supabase/functions   (fetch is mocked; no network is used)
import { assertEquals } from "jsr:@std/assert@1.0.6";

Deno.env.set("SUPABASE_URL", "https://example.supabase.co");
Deno.env.set("SUPABASE_ANON_KEY", "anon");
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service");

const realFetch = globalThis.fetch;
let row: Record<string, unknown> | null = null;
let chFetches = 0;

function mockFetch() {
  globalThis.fetch = ((input: Request | URL | string, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const j = (b: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(b), { status }));
    if (url.endsWith("/auth/v1/user")) return j({ id: "user-1", aud: "authenticated" });
    if (url.includes("/v1/account/83556/profile")) { chFetches++; return j({ accountId: 83556, username: "W", stats: { totalFcs: 2 } }); }
    if (url.includes("/rest/v1/profiles")) {
      if ((init?.method ?? "GET") === "GET") return Promise.resolve(new Response(JSON.stringify(row), { status: 200, headers: { "Content-Type": "application/json", "Content-Range": "0-0/*" } }));
      return j([], 204);
    }
    return j({ error: "unexpected " + url }, 500);
  }) as typeof fetch;
}
const req = () => new Request("https://x/ch-profile", { method: "POST", headers: { Authorization: "Bearer jwt", Accept: "application/vnd.pgrst.object+json" }, body: "{}" });

Deno.test("not linked: 404 not_linked", async () => {
  mockFetch(); row = { ch_account_id: null, ch_verified_at: null, ch_profile: null, ch_profile_at: null };
  const { handler } = await import("./index.ts");
  const r = await handler(req());
  assertEquals(r.status, 404);
  assertEquals((await r.json()).error, "not_linked");
  globalThis.fetch = realFetch;
});

Deno.test("fresh cache (under 60 s): no CH fetch", async () => {
  mockFetch(); chFetches = 0;
  const at = new Date(Date.now() - 10_000).toISOString();
  row = { ch_account_id: 83556, ch_verified_at: at, ch_profile: { cached: true }, ch_profile_at: at };
  const { handler } = await import("./index.ts");
  const b = await (await handler(req())).json();
  assertEquals(b.profile, { cached: true });
  assertEquals(chFetches, 0);
  globalThis.fetch = realFetch;
});

Deno.test("stale cache (over 60 s): fetches CH and returns the fresh profile", async () => {
  mockFetch(); chFetches = 0;
  const at = new Date(Date.now() - 120_000).toISOString();
  row = { ch_account_id: 83556, ch_verified_at: at, ch_profile: { cached: true }, ch_profile_at: at };
  const { handler } = await import("./index.ts");
  const b = await (await handler(req())).json();
  assertEquals(b.profile.stats.totalFcs, 2);
  assertEquals(chFetches, 1);
  globalThis.fetch = realFetch;
});
