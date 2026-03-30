"""Jobs collector registry.

Only LinkedIn and Indeed are active in the current pipeline. Legacy collectors
stay registered separately so old configs can be recognized and future
reactivation stays straightforward.
"""

from . import glassdoor, handshake, indeed, linkedin

ACTIVE_SOURCES = {
    "linkedin": linkedin,
    "indeed": indeed,
}

INACTIVE_SOURCES = {
    "glassdoor": glassdoor,
    "handshake": handshake,
}

SOURCES = {**ACTIVE_SOURCES, **INACTIVE_SOURCES}
