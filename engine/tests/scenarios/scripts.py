"""The ten scenarios of ADR 0025 and ADR 0029's Round matrix, as data.

Each turn is the person's opener followed by what a well-behaved assistant does (the scripted
model). The offline tests judge the scripts as written; the live runner sends only the openers
and observes what the real assistant does.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.scenarios.rules import Event


@dataclass(frozen=True)
class Scenario:
    """One scripted conversation.

    :param name: Short id, used on the command line and in the table.
    :param turns: The turns in order, each a list of events (see :mod:`tests.scenarios.rules`).
    :param max_asks: How many question cards the whole conversation may show.
    :param deliverable: ``"deck"`` when the scenario makes a new deck: the live runner then
        measures the message to its first save or ``deck_new``, in the turn or in the Helper it
        started. ``"page"`` for a new page. Either way the runner gives its requests a fresh
        subject on each run.
    """

    name: str
    turns: tuple[tuple[Event, ...], ...]
    max_asks: int = 0
    deliverable: str = ""


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "deck-from-csv",
        (
            (
                ("request", "Make a deck from sales.csv for Monday's review"),
                ("reply", "Building the review deck from sales.csv."),
                ("tool_call", "context_brief"),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("tool_call", "artifact_outline"),
                ("reply", "Here is the review deck, in Corporate Clean."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "deck-no-look",
        (
            (
                ("request", "Create some presentation about Acme vs Globex"),
                ("reply", "Building the Acme vs Globex comparison deck now."),
                ("tool_call", "context_brief"),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("tool_call", "artifact_outline"),
                ("reply", "Here is the comparison, in Pitch Deck."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "pick-question-continue",
        (
            (
                ("request", "Make a deck on our Q3 results; ask me which look first"),
                ("card", "look"),
            ),
            (
                ("answer", "Pitch Deck"),
                ("card", "data"),
            ),
            (
                ("answer", "Use mock numbers"),
                ("reply", "Building the Q3 deck with mock numbers."),
                ("tool_call", "context_brief"),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("tool_call", "artifact_outline"),
                ("reply", "Here is the Q3 deck with mock numbers."),
            ),
        ),
        max_asks=2,
        deliverable="deck",
    ),
    Scenario(
        "steer-mid-helper",
        (
            (
                ("request", "Compare five CRM vendors and write it up"),
                ("reply", "I'm comparing the five vendors now."),
                ("helper_started", ""),
            ),
            (
                ("steer", "Include pricing too"),
                ("message_helper", ""),
                ("reply", "Pricing goes in too."),
            ),
            (
                ("notice", "result"),
                ("reply", "The comparison is ready; HubSpot is cheapest."),
            ),
        ),
    ),
    Scenario(
        "one-step-lookup",
        (
            (
                ("message", "What time is it in Tokyo?"),
                ("tool_call", "web_search"),
                ("reply", "It is 9:14 pm in Tokyo."),
            ),
        ),
    ),
    Scenario(
        "restyle-existing-deck",
        (
            (
                ("request", "Pick a different theme for the board deck"),
                ("card", "look"),
            ),
            (
                ("answer", "Nord"),
                ("reply", "Switching the board deck to Nord."),
                ("tool_call", "deck_theme_set"),
                ("save", ""),
                ("reply", "The board deck is in Nord now."),
            ),
        ),
        max_asks=1,
    ),
    Scenario(
        "parallel-work",
        (
            (
                ("request", "Compare the pricing pages of these four vendors in a sheet"),
                ("reply", "I'll put the four vendors' pricing side by side in a sheet."),
                ("helper_started", ""),
                ("reply", "I'm reading the four pricing pages and building the sheet."),
            ),
        ),
    ),
    Scenario(
        "helper-flags",
        (
            (
                ("request", "Write a report on the EU AI Act obligations for our product"),
                ("reply", "I'm working through the obligations."),
                ("helper_started", ""),
            ),
            (
                ("notice", "flag"),
                ("reply", "Two sources disagree on the deadline; I'm using the official text."),
            ),
            (
                ("notice", "result"),
                ("reply", "The report is ready."),
            ),
        ),
    ),
    Scenario(
        "message-while-helpers-run",
        (
            (
                ("request", "Build a market-sizing deck for meal kits"),
                ("reply", "I'll size the meal-kit market and build the deck."),
                ("tool_call", "context_brief"),
                ("helper_started", ""),
            ),
            (
                ("request", "Also turn last week's notes into a one-page summary"),
                ("reply", "Writing the one-page summary of last week's notes."),
                ("tool_call", "sys_os_bash"),
                ("save", ""),
                ("reply", "Here is the summary; the deck is still being built."),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("tool_call", "artifact_outline"),
                ("reply", "The meal-kit deck is ready."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "cancelled-turn",
        (
            (
                ("request", "Make a 20-slide deck on the history of banking"),
                ("reply", "Building a 20-slide deck on the history of banking."),
                ("tool_call", "context_brief"),
                ("cancelled", ""),
            ),
        ),
        deliverable="deck",
    ),
)


#: ADR 0029's Round matrix: each Round from the person's message through settlement. The live
#: runner plays these after the ADR 0025 ten.
ROUNDS: tuple[Scenario, ...] = (
    Scenario(
        "round-deck-delegated",
        (
            (
                ("request", "Make a 5-slide deck about Uruguay"),
                ("reply", "I'll put together a 5-slide deck on Uruguay."),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("reply", "Your Uruguay deck is ready: history, people, economy and football."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "round-deck-direct",
        (
            (
                ("request", "Make a 3-slide deck about Lisbon"),
                ("reply", "Making a 3-slide deck on Lisbon now."),
                ("tool_call", "deck_new"),
                ("tool_call", "deck_check"),
                ("save", ""),
                ("reply", "Here is the Lisbon deck."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "round-landing-page",
        (
            (
                ("request", "Build a landing page for a coffee subscription"),
                ("reply", "Building the coffee subscription landing page."),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("reply", "The landing page is ready, with plans, pricing and a sign-up form."),
            ),
        ),
        deliverable="page",
    ),
    Scenario(
        "round-two-deliverables",
        (
            (
                ("request", "Make a deck on our Q3 numbers and a one-page summary of them"),
                ("reply", "I'll make the Q3 deck and the one-page summary."),
                ("helper_started", ""),
                ("tool_call", "sys_os_bash"),
                ("save", ""),
                ("reply", "The summary is ready; the deck is on its way."),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("reply", "The Q3 deck is ready too."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "round-stop-follow-up",
        (
            (
                ("request", "Make a 10-slide deck about the history of jazz"),
                ("reply", "Starting a 10-slide deck on the history of jazz."),
                ("helper_started", ""),
                ("cancelled", ""),
            ),
            (
                ("message", "Did it finish?"),
                ("reply", "No. You stopped it before any slides were saved."),
            ),
        ),
        deliverable="deck",
    ),
    Scenario(
        "round-repeat-ask",
        (
            (
                ("request", "Make a 5-slide deck about Uruguay"),
                ("reply", "I'll put together a 5-slide deck on Uruguay."),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("reply", "Your Uruguay deck is ready."),
            ),
            (
                ("request", "Make that Uruguay deck again, with a slide on football"),
                ("reply", "Adding a football slide to the Uruguay deck from earlier."),
                ("tool_call", "recall"),
                ("helper_started", ""),
            ),
            (
                ("notice", "result"),
                ("helper_save", ""),
                ("check", "deck_check"),
                ("reply", "The Uruguay deck now has a football slide."),
            ),
        ),
        deliverable="deck",
    ),
)
