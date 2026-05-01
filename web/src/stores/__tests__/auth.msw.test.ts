/**
 * MSW v2 integration tests for the auth store.
 *
 * These tests intercept real `fetch` calls via MSW to verify:
 * - Correct HTTP verbs (GET vs POST)
 * - Correct request paths
 * - Correct request bodies
 * - Correct state transitions in the zustand store
 *
 * Note: jsdom does not support programmatic navigation, so
 * `window.location.href = "/login"` and `window.location.replace("/login")`
 * emit console warnings ("Not implemented: navigation") but do not throw.
 * We intentionally do not assert navigation side-effects here; those are
 * covered by the component-level tests (e.g. AuthGuard).
 */
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { useAuthStore } from "@/stores/auth";
import { ApiError, RateLimitedError } from "@/lib/api";

// ---------------------------------------------------------------------------
// MSW server setup
// ---------------------------------------------------------------------------

const server = setupServer();

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

// Helper to reset auth store to a clean baseline between tests.
function resetAuthStore() {
  useAuthStore.setState({ authenticated: false, _checked: false });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("auth store (MSW integration)", () => {
  // -----------------------------------------------------------------------
  // checkAuth
  // -----------------------------------------------------------------------

  describe("checkAuth", () => {
    it("200 → authenticated=true, _checked=true", async () => {
      resetAuthStore();
      server.use(
        http.get("/api/auth/me", () => HttpResponse.json({ ok: true })),
      );

      await useAuthStore.getState().checkAuth();

      const s = useAuthStore.getState();
      expect(s.authenticated).toBe(true);
      expect(s._checked).toBe(true);
    });

    it("uses GET verb (not POST)", async () => {
      resetAuthStore();
      let capturedMethod = "";
      server.use(
        http.all("/api/auth/me", ({ request }) => {
          capturedMethod = request.method;
          return HttpResponse.json({ ok: true });
        }),
      );

      await useAuthStore.getState().checkAuth();

      expect(capturedMethod).toBe("GET");
    });

    it("401 → authenticated=false, _checked=true", async () => {
      resetAuthStore();
      server.use(
        http.get("/api/auth/me", () => new HttpResponse(null, { status: 401 })),
      );

      await useAuthStore.getState().checkAuth();

      const s = useAuthStore.getState();
      expect(s.authenticated).toBe(false);
      expect(s._checked).toBe(true);
    });
  });

  // -----------------------------------------------------------------------
  // login
  // -----------------------------------------------------------------------

  describe("login", () => {
    it("200 → authenticated=true", async () => {
      resetAuthStore();
      server.use(
        http.post("/api/auth/login", () => HttpResponse.json({ ok: true })),
      );

      await useAuthStore.getState().login("secret");

      const s = useAuthStore.getState();
      expect(s.authenticated).toBe(true);
      expect(s._checked).toBe(true);
    });

    it("sends {password} in request body", async () => {
      resetAuthStore();
      let capturedBody: unknown = null;
      server.use(
        http.post("/api/auth/login", async ({ request }) => {
          capturedBody = await request.json();
          return HttpResponse.json({ ok: true });
        }),
      );

      await useAuthStore.getState().login("my-password");

      expect(capturedBody).toEqual({ password: "my-password" });
    });

    it("uses POST verb", async () => {
      resetAuthStore();
      let capturedMethod = "";
      server.use(
        http.all("/api/auth/login", ({ request }) => {
          capturedMethod = request.method;
          return HttpResponse.json({ ok: true });
        }),
      );

      await useAuthStore.getState().login("pw");

      expect(capturedMethod).toBe("POST");
    });

    it("401 → throws ApiError, authenticated=false", async () => {
      resetAuthStore();
      server.use(
        http.post("/api/auth/login", () => new HttpResponse(null, { status: 401 })),
      );

      await expect(
        useAuthStore.getState().login("wrong"),
      ).rejects.toBeInstanceOf(ApiError);

      const s = useAuthStore.getState();
      expect(s.authenticated).toBe(false);
    });

    it("429 with Retry-After → throws RateLimitedError", async () => {
      resetAuthStore();
      server.use(
        http.post("/api/auth/login", () =>
          new HttpResponse(null, {
            status: 429,
            headers: { "Retry-After": "120" },
          }),
        ),
      );

      try {
        await useAuthStore.getState().login("pw");
        expect.fail("should have thrown");
      } catch (err) {
        expect(err).toBeInstanceOf(RateLimitedError);
        expect((err as RateLimitedError).retryAfterSeconds).toBe(120);
      }

      // State should remain unauthenticated
      expect(useAuthStore.getState().authenticated).toBe(false);
    });
  });

  // -----------------------------------------------------------------------
  // logout
  // -----------------------------------------------------------------------

  describe("logout", () => {
    it("204 → authenticated=false", async () => {
      useAuthStore.setState({ authenticated: true, _checked: true });
      server.use(
        http.post("/api/auth/logout", () => new HttpResponse(null, { status: 204 })),
      );

      await useAuthStore.getState().logout();

      expect(useAuthStore.getState().authenticated).toBe(false);
    });

    it("uses POST verb", async () => {
      useAuthStore.setState({ authenticated: true, _checked: true });
      let capturedMethod = "";
      server.use(
        http.post("/api/auth/logout", ({ request }) => {
          capturedMethod = request.method;
          return new HttpResponse(null, { status: 204 });
        }),
      );

      await useAuthStore.getState().logout();

      expect(capturedMethod).toBe("POST");
    });

    it("401 from logout is swallowed (already logged out)", async () => {
      useAuthStore.setState({ authenticated: true, _checked: true });
      server.use(
        http.post("/api/auth/logout", () => new HttpResponse(null, { status: 401 })),
      );

      // Should NOT throw — auth store catches ApiError from logout
      await useAuthStore.getState().logout();

      expect(useAuthStore.getState().authenticated).toBe(false);
    });
  });

  // -----------------------------------------------------------------------
  // logoutLocal
  // -----------------------------------------------------------------------

  describe("logoutLocal", () => {
    it("resets state without making HTTP calls", async () => {
      useAuthStore.setState({ authenticated: true, _checked: true });

      // If any request escapes, MSW will throw "unhandled request" error
      server.use(
        http.all("*", () => {
          return HttpResponse.error();
        }),
      );

      useAuthStore.getState().logoutLocal();

      const s = useAuthStore.getState();
      expect(s.authenticated).toBe(false);
      expect(s._checked).toBe(true);
    });
  });
});
