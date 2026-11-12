"""Reach rules (ADR 0038, "what the person says is a rule"): they beat the rhythm."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnigent.superchat.rhythm import reach
from omnigent.superchat.rhythm.reach import Rule, clean
from omnigent.superchat.rhythm.score import NOT, Model, Prefs, Tally
from omnigent.superchat.rhythm.service import Rhythm
from omnigent.superchat.rhythm.store import SqlAlchemyRhythmStore

MON_10 = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
SAT_10 = datetime(2026, 10, 31, 10, 0, tzinfo=UTC)


def _rule(kind: str, params: dict, *, ends: datetime | None = None, at: int = 0) -> Rule:
    cleaned = clean(kind, params)
    assert isinstance(cleaned, dict), cleaned
    return Rule(
        id=f"{kind}{at}",
        kind=kind,
        params=cleaned,
        created_at=at,
        expires_at=int(ends.timestamp()) if ends else None,
    )


def test_a_pause_beats_urgency_learning_and_exploration_until_it_ends() -> None:
    ends = MON_10 + timedelta(hours=3)
    pause = _rule("pause", {}, ends=ends)
    # Learned open, nothing sent for days (exploring), urgent: the pause still holds.
    model = Model(
        prefs=Prefs(level="high"),
        tallies={"morning:weekday:not": Tally(yes=5.0)},
        learned=True,
        rules=[pause],
    )
    assert not model.good(MON_10, NOT, urgent=True)
    assert model.next_slot(MON_10, NOT) == ends
    assert not reach.paused([pause], MON_10.timestamp(), brief=True)
    assert reach.paused([_rule("pause", {"brief": True})], MON_10.timestamp(), brief=True)


def test_a_spoken_level_replaces_the_stored_one() -> None:
    sent = [(MON_10.timestamp() - 3600, "x", "a")]
    assert not Model(
        prefs=Prefs(level="high"), sent=sent, rules=[_rule("level", {"per_day": 1})]
    ).good(MON_10, NOT)
    assert Model(prefs=Prefs(level="low"), sent=sent, rules=[_rule("level", {"per_day": 6})]).good(
        MON_10, NOT
    )
    assert not Model(prefs=Prefs(), rules=[_rule("level", {"per_day": 0})]).good(
        MON_10, NOT, urgent=True
    )


def test_check_in_times_and_days_replace_the_window() -> None:
    model = Model(prefs=Prefs(), rules=[_rule("hours", {"at": ["09:00", "16:00"]})])
    assert model.good(MON_10.replace(hour=9, minute=15), NOT)
    assert not model.good(MON_10, NOT)
    slot = model.next_slot(MON_10, NOT)
    assert slot is not None and slot.astimezone(UTC).hour == 16
    weekend = Model(prefs=Prefs(), rules=[_rule("hours", {"days": ["sat"], "from": "09:00"})])
    assert weekend.good(SAT_10, NOT)
    assert not weekend.good(MON_10, NOT)


def test_only_for_and_mute_read_who_and_what() -> None:
    dana = [_rule("only_for", {"people": ["Dana"], "urgency": "now"})]
    model = Model(prefs=Prefs(), rules=dana)
    assert model.good(MON_10, NOT, "deal", senders=["Dana Levi <dana@example.com>", None])
    assert not model.good(MON_10, NOT, "deal", senders=["Danail <d@example.com>"])
    assert not model.good(MON_10, NOT, "deal")
    assert model.good(MON_10, NOT, "deal", urgent=True)
    assert Model(prefs=Prefs(), rules=[_rule("only_for", {"topics": ["Polaris"]})]).good(
        MON_10, NOT, "polaris"
    )
    muted = Model(
        prefs=Prefs(),
        rules=[_rule("mute", {"topic": "polaris"}), _rule("mute", {"person": "Sam"}, at=1)],
    )
    assert not muted.good(MON_10, NOT, "polaris", urgent=True)
    assert not muted.good(MON_10, NOT, "deal", senders=["Sam <sam@example.com>"])
    assert muted.good(MON_10, NOT, "deal", senders=["Sam <sam@example.com>", None])


def test_rules_are_kept_replaced_ended_and_survive_start_over(db_uri: str) -> None:
    rhythm = Rhythm(SqlAlchemyRhythmStore(db_uri), None)
    assert isinstance(rhythm.add_rule("me", "level", {"per_day": 7}, "lots"), str)
    first = rhythm.add_rule("me", "pause", {}, "don't bother me")
    assert isinstance(first, dict) and "brief still comes" in first["rule"]["line"]
    second = rhythm.add_rule("me", "pause", {"brief": True}, "nothing at all, not even the brief")
    assert isinstance(second, dict) and second["replaced"][0]["id"] == first["rule"]["id"]
    rhythm.add_rule("me", "mute", {"topic": "polaris"}, "nothing about Polaris")
    assert rhythm.brief_paused("me")
    rhythm.reset("me")
    assert [r["kind"] for r in rhythm.rules("me")] == ["pause", "mute"]
    assert not rhythm.good_moment("me", MON_10, NOT)
    removed = rhythm.remove_rules("me", kind="pause")
    assert [r["kind"] for r in removed] == ["pause"] and not rhythm.brief_paused("me")
    # An ended rule is ignored and cleaned on read.
    store = rhythm._store
    store.add_rule("me", _rule("pause", {}, ends=datetime(2020, 1, 1, tzinfo=UTC), at=5))
    assert [r.kind for r in store.rules("me")] == ["mute"]
    assert [r.kind for r in store.rules("me", now=0)] == ["mute"]


def test_only_the_persons_own_conversation_writes_a_rule(db_uri: str) -> None:
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from omnigent.errors import OmnigentError
    from omnigent.superchat.rhythm.routes import create_rhythm_router

    chat, hidden = "a" * 32, "b" * 32
    convs = {
        chat: SimpleNamespace(
            parent_conversation_id=None, labels={"omnigent.context.mode": "superside-chat"}
        ),
        hidden: SimpleNamespace(
            parent_conversation_id=chat,
            labels={"omnigent.context.mode": "superside-chat", "omnigent.subagent": "x"},
        ),
    }
    app = FastAPI()
    app.exception_handler(OmnigentError)(lambda _r, e: JSONResponse({"error": str(e)}, 400))
    app.include_router(
        create_rhythm_router(
            Rhythm(SqlAlchemyRhythmStore(db_uri), None),
            auth_provider=None,
            conversation_store=SimpleNamespace(get_conversation=convs.get),
        ),
        prefix="/v1",
    )
    client = TestClient(app)
    body = {"action": "add", "kind": "pause", "params": {}, "said": "not today"}
    assert (
        client.post("/v1/me/reach-rules", json={**body, "session_id": hidden}).status_code == 400
    )
    assert client.post("/v1/me/reach-rules", json={**body, "session_id": chat}).status_code == 200
    (rule,) = client.get("/v1/me/reach-rules").json()["rules"]
    assert rule["kind"] == "pause"
    assert client.delete(f"/v1/me/reach-rules/{rule['id']}").status_code == 200
    assert client.get("/v1/me/reach-rules").json()["rules"] == []


def test_a_changed_rule_makes_the_lookout_look_again_now(db_uri: str) -> None:
    import time
    from types import SimpleNamespace

    from omnigent.superchat.lookout.service import Lookout
    from omnigent.superchat.lookout.store import SqlAlchemyLookoutStore
    from omnigent.superchat.rhythm.service import RULES_CHANGED

    lookout = Lookout(
        store=SqlAlchemyLookoutStore(db_uri),
        tasks=SimpleNamespace(get_owner_preferences=lambda _owner: None),
        conversations=None,
    )
    now = int(time.time())
    held = {"owner": "me", "kind": "library_file", "subject": "Q3", "dedupe_key": "f:1"}
    lookout.add_signal({**held, "due_at": now - 60, "stale_after": now + 86400})
    lookout.store.set_next_wake("me", now + 8 * 86400)  # held behind a pause
    RULES_CHANGED.append(lookout.on_rules_changed)
    try:
        rhythm = Rhythm(SqlAlchemyRhythmStore(db_uri), None)
        rhythm.add_rule("me", "pause", {}, "quiet please")
        rhythm.remove_rules("me", kind="pause")
    finally:
        RULES_CHANGED.remove(lookout.on_rules_changed)
    assert any(owner == "me" for _ws, owner in lookout.store.due_owners(now + 1))
