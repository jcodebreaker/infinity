import { expect, it } from "vitest";
import {
  CONNECTION_FAILURE_THRESHOLD,
  type ConnectionLatch,
  INITIAL_CONNECTION_LATCH,
  nextConnectionLatch,
} from "./connectionStatus";

it("starts connecting before any poll has ever landed", () => {
  expect(INITIAL_CONNECTION_LATCH.status).toBe("connecting");
});

it("latches Connected the moment any poll lands, and resets the failure streak", () => {
  const latch = nextConnectionLatch(INITIAL_CONNECTION_LATCH, { status: "ready" });
  expect(latch).toEqual({ status: "connected", consecutiveFailures: 0 });
});

it("NOT_IMPLEMENTED ('unavailable') still counts as having reached the server", () => {
  expect(nextConnectionLatch(INITIAL_CONNECTION_LATCH, { status: "unavailable" }).status).toBe(
    "connected",
  );
});

it("stays Connected through a single failed round — one dropped request doesn't flicker it", () => {
  const connected = { status: "connected" as const, consecutiveFailures: 0 };
  const after1 = nextConnectionLatch(connected, { status: "error" });
  expect(after1.status).toBe("connected");
  expect(after1.consecutiveFailures).toBe(1);
});

it(`only lets go of Connected after ${CONNECTION_FAILURE_THRESHOLD} consecutive failed rounds`, () => {
  let latch: ConnectionLatch = { status: "connected", consecutiveFailures: 0 };
  for (let round = 1; round < CONNECTION_FAILURE_THRESHOLD; round++) {
    latch = nextConnectionLatch(latch, { status: "error" }, { status: "loading" });
    expect(latch.status).toBe("connected");
  }
  latch = nextConnectionLatch(latch, { status: "error" }, { status: "loading" });
  expect(latch.status).toBe("connecting");
  expect(latch.consecutiveFailures).toBe(CONNECTION_FAILURE_THRESHOLD);
});

it("any one poll landing is enough to stay Connected even if another is failing", () => {
  const latch = nextConnectionLatch(
    { status: "connected", consecutiveFailures: CONNECTION_FAILURE_THRESHOLD - 1 },
    { status: "error" },
    { status: "ready" },
  );
  expect(latch).toEqual({ status: "connected", consecutiveFailures: 0 });
});

it("stays Connecting while already Connecting and polls keep failing", () => {
  const connecting = { status: "connecting" as const, consecutiveFailures: 5 };
  const latch = nextConnectionLatch(connecting, { status: "error" });
  expect(latch.status).toBe("connecting");
});
