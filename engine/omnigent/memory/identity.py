"""The person's own organisation, as far as memory and their address say (ADR 0035, amended).

Topic watch treats the person's own organisation as a common term (every work email names it),
and the quiet pass is told it so topics are never watched by it. Both read it here.

Derivation is deliberately conservative, code only:

- **Organisation names** come from the person's own ``fact`` claims (written "The user ...") that
  say where they work: "works at / for / with X", "employed at / by X", "employee of X",
  "employer / company / organisation / firm is X", "is a <role> at X". ``X`` is the run of
  capitalised words (at most four) right after the phrase; anything else is ignored.
- **Domains** come from the person's own addresses: their owner id when it is an address (header
  and OIDC modes) and an address a fact claim gives as "the user's (work) email". Consumer mail
  providers are skipped. The domain's name label counts as an organisation word too (``northwind``
  from ``northwind.com``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.memory.service import MemoryService

_logger = logging.getLogger(__name__)

#: Mail providers whose domain says nothing about where the person works.
CONSUMER_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "yandex.com",
        "zoho.com",
        "fastmail.com",
        "hey.com",
    }
)
#: Second-level labels of country suffixes such as ``co.uk`` or ``com.au``.
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc"})

_NAME = r"([A-Z0-9][\w&.'-]*(?:\s+(?:&\s+|of\s+|and\s+)?[A-Z0-9][\w&.'-]*){0,3})"
_ORG_PATTERNS = (
    re.compile(rf"\b(?:works?|working|worked)\s+(?:at|for|with)\s+(?:the\s+)?{_NAME}"),
    re.compile(rf"\bemploy(?:ed|ee)\s+(?:at|by|of|with)\s+(?:the\s+)?{_NAME}"),
    re.compile(rf"\b(?i:employer|company|organi[sz]ation|firm)\s+is\s+(?:the\s+)?{_NAME}"),
    re.compile(rf"\bis\s+(?:a|an|the)\s+[^.,;\n]{{1,40}}?\s+at\s+(?:the\s+)?{_NAME}"),
)
_EMAIL = re.compile(r"[\w.+'-]+@([\w-]+(?:\.[\w-]+)+)")
_OWN_EMAIL = re.compile(
    r"\b(?:user's|their|my)\s+(?:work\s+)?e-?mail(?:\s+address)?\s+is\s+([\w.+'-]+@[\w.-]+\w)",
    re.IGNORECASE,
)


def org_names(claims: Iterable[str]) -> list[str]:
    """Organisation names the person's fact *claims* say they work at, first seen first."""
    found: list[str] = []
    for text in claims:
        for pattern in _ORG_PATTERNS:
            for hit in pattern.finditer(text):
                name = hit.group(1).rstrip(".'-")
                if name and name.casefold() not in {n.casefold() for n in found}:
                    found.append(name)
    return found


def own_addresses(owner: str, claims: Iterable[str]) -> list[str]:
    """The person's own addresses: *owner* when it is one, and any a fact claim states."""
    out = [owner] if _EMAIL.fullmatch(owner.strip()) else []
    for text in claims:
        out.extend(m.group(1) for m in _OWN_EMAIL.finditer(text))
    return list(dict.fromkeys(a.strip().casefold() for a in out))


def org_domains(addresses: Iterable[str]) -> list[str]:
    """The work domains of *addresses* (consumer mail providers left out)."""
    domains: list[str] = []
    for address in addresses:
        found = _EMAIL.fullmatch(address.strip())
        if found is None:
            continue
        domain = found.group(1).casefold()
        if domain not in CONSUMER_DOMAINS and domain not in domains:
            domains.append(domain)
    return domains


def domain_label(domain: str) -> str:
    """The name part of *domain*: ``northwind`` from ``northwind.com``, ``acme`` from ``mail.acme.co.uk``."""
    labels = domain.casefold().split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return labels[-3]
    return labels[-2] if len(labels) >= 2 else labels[0]


def _fact_texts(owner: str, service: MemoryService | None) -> list[str]:
    if service is None:
        from omnigent.runtime import get_memory_service

        service = get_memory_service()
    if service is None:
        return []
    try:
        claims: Sequence[dict[str, Any]] = service.list_claims(owner, kinds=["fact"])
    except Exception:
        _logger.warning("own organisation: reading the facts of %s failed", owner, exc_info=True)
        return []
    return [str(c.get("text") or "") for c in claims if c.get("status") != "expired"]


def own_organisation(owner: str, *, service: MemoryService | None = None) -> list[str]:
    """The person's own organisation as they would read it: names from memory, then domains.

    Blocking (reads memory). Empty when nothing is known.
    """
    facts = _fact_texts(owner, service)
    return [*org_names(facts), *org_domains(own_addresses(owner, facts))]


def own_org_terms(owner: str, *, service: MemoryService | None = None) -> list[str]:
    """What names the person's own organisation in their mail: names, domains, domain labels.

    Blocking (reads memory). Topic watch treats these as common terms.
    """
    facts = _fact_texts(owner, service)
    domains = org_domains(own_addresses(owner, facts))
    terms = [*org_names(facts), *domains, *(domain_label(d) for d in domains)]
    return list(dict.fromkeys(t for t in terms if t))
