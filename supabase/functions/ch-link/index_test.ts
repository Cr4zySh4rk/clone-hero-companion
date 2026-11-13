// Run: deno test --allow-env --allow-net=none supabase/functions   (fetch is mocked; no network is used)
import { assertEquals } from "jsr:@std/assert@1.0.6";

Deno.env.set("SUPABASE_URL", "https://example.supabase.co");
Deno.env.set("SUPABASE_ANON_KEY", "anon");
Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service");

const calls: { url: string; method: string; auth: string | null }[] = [];
let chMe = { ok: true, body: { accountId: 83556, username: "W4RL0CKM0NK3Y" } };
const realFetch = globalThis.fetch;

function mockFetch() {
  globalThis.fetch = ((input: Request | URL | string, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const headers = new Headers(init?.headers);
    calls.push({ url, method: init?.method ?? "GET", auth: headers.get("Authorization") });
    const j = (b: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(b), { status }));
    if (url.endsWith("/auth/v1/user")) return j({ id: "user-1", aud: "authenticated" });
    if (url.endsWith("/v1/account/@me")) return chMe.ok ? j(chMe.body) : j({ message: "no" }, 401);
    if (url.endsWith("/v1/account/83556/profile")) return j({ accountId: 83556, username: "W4RL0CKM0NK3Y", stats: { totalFcs: 1 } });
    if (url.endsWith("/v2/auth/logout")) return j({});
    if (url.includes("/rest/v1/profiles")) return j([], 201);   // upsert / count
    return j({ error: "unexpected " + url }, 500);
  }) as typeof fetch;
}

Deno.test("OPTIONS answers CORS for the site origins only", async () => {
  const { handler } = await import("./index.ts");
  for (const [origin, allowed] of [["https://cloneherocompanion.com", true], ["http://127.0.0.1:47823", true], ["https://evil.example", false]] as const) {
    const r = await handler(new Request("https://x/ch-link", { method: "OPTIONS", headers: { Origin: origin } }));
    assertEquals(r.status, 204);
    assertEquals(r.headers.get("Access-Control-Allow-Origin"), allowed ? origin : null);
  }
});

Deno.test("invalid CH session: 401 ch_session_invalid, and the CH session is still logged out", async () => {
  mockFetch(); calls.length = 0; chMe = { ok: false, body: {} as never };
  const { handler } = await import("./index.ts");
  const r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt", Origin: "https://cloneherocompanion.com" },
    body: JSON.stringify({ sessionToken: "SECRET-TOKEN" }),
  }));
  assertEquals(r.status, 401);
  assertEquals((await r.json()).error, "ch_session_invalid");
  assertEquals(calls.some((c) => c.url.endsWith("/v2/auth/logout") && c.auth === "Bearer SECRET-TOKEN"), true);
  globalThis.fetch = realFetch;
});

Deno.test("valid CH session: returns the account and always logs out", async () => {
  mockFetch(); calls.length = 0; chMe = { ok: true, body: { accountId: 83556, username: "W4RL0CKM0NK3Y" } };
  const { handler } = await import("./index.ts");
  const r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt" }, body: JSON.stringify({ sessionToken: "SECRET-TOKEN" }),
  }));
  assertEquals(r.status, 200);
  const b = await r.json();
  assertEquals(b.accountId, 83556);
  assertEquals(b.username, "W4RL0CKM0NK3Y");
  assertEquals(calls.at(-1)!.url.endsWith("/v2/auth/logout"), true);
  assertEquals(JSON.stringify(b).includes("SECRET-TOKEN"), false);
  globalThis.fetch = realFetch;
});

Deno.test("keepSession: links, reports sessionKept, and does NOT log the CH session out", async () => {
  mockFetch(); calls.length = 0; chMe = { ok: true, body: { accountId: 83556, username: "W4RL0CKM0NK3Y" } };
  const { handler } = await import("./index.ts");
  const r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt" }, body: JSON.stringify({ sessionToken: "SECRET-TOKEN", keepSession: true }),
  }));
  assertEquals(r.status, 200);
  const b = await r.json();
  assertEquals(b.accountId, 83556);
  assertEquals(b.sessionKept, true);
  assertEquals(calls.some((c) => c.url.endsWith("/v1/account/@me") && c.auth === "Bearer SECRET-TOKEN"), true);
  assertEquals(calls.some((c) => c.url.endsWith("/v2/auth/logout")), false);
  assertEquals(JSON.stringify(b).includes("SECRET-TOKEN"), false);
  globalThis.fetch = realFetch;
});

Deno.test("keepSession with an invalid CH session: 401, and the session is still logged out", async () => {
  mockFetch(); calls.length = 0; chMe = { ok: false, body: {} as never };
  const { handler } = await import("./index.ts");
  const r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt" }, body: JSON.stringify({ sessionToken: "SECRET-TOKEN", keepSession: true }),
  }));
  assertEquals(r.status, 401);
  assertEquals(calls.some((c) => c.url.endsWith("/v2/auth/logout") && c.auth === "Bearer SECRET-TOKEN"), true);
  globalThis.fetch = realFetch;
});

Deno.test("without keepSession the answer says sessionKept: false", async () => {
  mockFetch(); calls.length = 0; chMe = { ok: true, body: { accountId: 83556, username: "W4RL0CKM0NK3Y" } };
  const { handler } = await import("./index.ts");
  const r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt" }, body: JSON.stringify({ sessionToken: "SECRET-TOKEN" }),
  }));
  assertEquals((await r.json()).sessionKept, false);
  globalThis.fetch = realFetch;
});

Deno.test("unlink with the kept sessionToken logs that CH session out; without it, no logout call", async () => {
  const { handler } = await import("./index.ts");
  mockFetch(); calls.length = 0;
  let r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt" }, body: JSON.stringify({ unlink: true, sessionToken: "SECRET-TOKEN" }),
  }));
  assertEquals(r.status, 200);
  assertEquals((await r.json()).unlinked, true);
  assertEquals(calls.some((c) => c.url.endsWith("/v2/auth/logout") && c.auth === "Bearer SECRET-TOKEN"), true);
  calls.length = 0;
  r = await handler(new Request("https://x/ch-link", {
    method: "POST", headers: { Authorization: "Bearer jwt" }, body: JSON.stringify({ unlink: true }),
  }));
  assertEquals(r.status, 200);
  assertEquals(calls.some((c) => c.url.endsWith("/v2/auth/logout")), false);
  globalThis.fetch = realFetch;
});

Deno.test("no Authorization header: 401", async () => {
  mockFetch();
  const { handler } = await import("./index.ts");
  const r = await handler(new Request("https://x/ch-link", { method: "POST", body: "{}" }));
  assertEquals(r.status, 401);
  globalThis.fetch = realFetch;
});
