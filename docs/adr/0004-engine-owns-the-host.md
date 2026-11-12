# The engine owns the host; Superchat only relays

Turns now run on the Omnigent engine, inside the Muse's host. Superchat and the engine each kept their own record of which container that host was, and the records drifted: the engine's sandbox provider re-provisioned the container, Superchat's `host.providerRef` kept the old id, and the idle jobs all acted on a container that no longer existed. We decided the engine owns the host end to end: which container it is and whether its runner is ready. Superchat's API asks the engine and holds no host state of its own. The sandbox supervisor stays as the plain Docker backend behind the engine's provider, and only the engine calls it.

## Considered options

- **Superchat owns the host, the engine borrows it.** Rejected: everything is moving under the engine, and keeping Superchat's record authoritative would mean the engine reports container changes back into Superchat's database, which is exactly the coupling that drifted.
- **Split: engine runs turns, Superchat keeps the host record and syncs from the engine's host record.** Rejected: two records of one thing is the bug we are fixing.

## Consequences

- The host stays on; nothing stops it when idle. The Pi-era idle-sleep job goes away.

## Amendments

**Amended 2026-10-19 (fix wave): readiness is the real connection.** With one engine (the default) a host is connected exactly when this engine's `HostRegistry` holds its tunnel; an "online" hosts row with no connection here is left over from a gone connection, never another replica's. Such a row only buys the host a 15 s reconnect window (`_HOST_RECONNECT_GRACE_S`), then it is relaunched; nothing is rejected as "on another replica". A launch or wake waits on `HostRegistry.wait_for` (the registry's connect signal, bounded) before starting the runner and fails loudly if the host never connects; the wake's polling loop is gone. `OMNIGENT_MULTI_REPLICA=1` restores the cross-replica checks for several engines on one database (`server/host_registry.py` `multi_replica`, `server/routes/_sessions/orchestration.py`, `server/routes/sessions/routes_events.py`). After an engine restart the row read "online" for up to 90 s and every message was rejected.
