import { describe, expect, it } from "vitest";
import { isBootstrapRoute } from "./bootstrap-target";

describe("bootstrap route", () => {
  it("primes app routes", () => {
    expect(isBootstrapRoute("/app")).toBe(true);
    expect(isBootstrapRoute("/app/some-muse")).toBe(true);
    expect(isBootstrapRoute("/app/artifacts/abc")).toBe(true);
  });

  it("does not prime other routes", () => {
    expect(isBootstrapRoute("/")).toBe(false);
    expect(isBootstrapRoute("/application")).toBe(false);
  });
});
