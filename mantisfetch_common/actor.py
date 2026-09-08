"""Who wrote a document — read off the request, never off the body.

SharedSpecs IRP 20260908 (decision §2, ruling ①). Two headers carry it:

* ``X-RFAI-Actor-ID`` — "who this turn is on behalf of", in the 20260607 D1
  form ``<kind>:<id>`` with kind ∈ {human, service, agent}. NodalOS only ever
  sends the ``human:`` form and omits the header otherwise.
* ``X-NodalOS-Caller-Agent-ID`` — the hosted agent that made the call, as a
  bare alias with no prefix.

The fill rule::

    created_by  = actor header            iff it is human:<sub>        else None
    created_via = "agent:" + caller alias if that header is present
                = actor header            elif it is any non-human form
                = None

These are audit fields, not a gate: a missing header records ``None`` and the
write goes ahead. What is recorded is what an *authenticated* caller asserted —
under a shared bearer token that is "one of N products", so nothing here may be
used to authorise anything (decision §2, ruling ②). The ``agent:`` prefix is
the only thing this module adds; the alias is not normalised further.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)

ACTOR_HEADER = "X-RFAI-Actor-ID"
CALLER_AGENT_HEADER = "X-NodalOS-Caller-Agent-ID"
# What a front-end that sits between the caller and the document store may pass
# through, and nothing else — never Authorization.
FORWARDED_HEADERS = (ACTOR_HEADER, CALLER_AGENT_HEADER)

_KINDS = ("human", "service", "agent")

Actor = tuple[str | None, str | None]
"""``(created_by, created_via)``."""


def _lookup(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive header fetch; a blank value counts as absent."""
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        wanted = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == wanted:
                value = candidate
                break
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _well_formed(value: str | None) -> str | None:
    """The value if it is ``<kind>:<non-empty id>`` with a known kind, else None."""
    if value is None:
        return None
    kind, sep, ident = value.partition(":")
    if sep and kind in _KINDS and ident.strip():
        return value
    logger.warning("ignoring malformed %s value (expected <kind>:<id>): %r", ACTOR_HEADER, value)
    return None


def actor_from_headers(headers: Mapping[str, str] | None) -> Actor:
    """Apply the fill rule to a request's headers."""
    actor = _well_formed(_lookup(headers, ACTOR_HEADER))
    alias = _lookup(headers, CALLER_AGENT_HEADER)

    created_by = actor if actor is not None and actor.startswith("human:") else None
    if alias is not None:
        created_via: str | None = f"agent:{alias}"
    elif actor is not None and not actor.startswith("human:"):
        created_via = actor
    else:
        created_via = None
    return created_by, created_via


def actor_label(actor: Actor | None) -> str:
    """One token naming the caller for a log line; ``unknown`` when nothing was sent."""
    if actor is None:
        return "unknown"
    created_by, created_via = actor
    return created_by or created_via or "unknown"


def forwardable_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """The identity headers present on ``headers``, ready to send onward.

    For a front-end (the MCP server) that calls the document store on the
    caller's behalf: only these two travel; the caller's own credentials do not.
    """
    out: dict[str, str] = {}
    for name in FORWARDED_HEADERS:
        value = _lookup(headers, name)
        if value is not None:
            out[name] = value
    return out
