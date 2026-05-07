"""Base class every source plugin implements."""

from __future__ import annotations

import abc
import logging
from typing import ClassVar

from ..models import Listing, SearchQuery

log = logging.getLogger(__name__)


class BaseSource(abc.ABC):
    """A pluggable rental source.

    Subclasses must set ``name`` and ``trust`` and implement ``search``.
    Implementations should be polite (use ``utils.http.polite_sleep``) and
    must catch their own per-listing errors so a single bad row does not
    poison the whole batch. Errors that prevent ANY result should raise.
    """

    name: ClassVar[str] = "base"
    trust: ClassVar[float] = 0.5  # 0..1, contributes to ranker.source_trust

    def __init__(self, *, debug: bool = False) -> None:
        self.debug = debug

    @abc.abstractmethod
    async def search(self, query: SearchQuery) -> list[Listing]:
        """Return a list of normalized listings matching the query.

        Implementations may return [] if the source has nothing to add. They
        must NOT raise for empty results, only for hard infrastructure
        failures (unreachable host, auth expired, etc.).
        """

    def _log(self, msg: str, *args: object) -> None:
        log.info("[%s] " + msg, self.name, *args)

    def _warn(self, msg: str, *args: object) -> None:
        log.warning("[%s] " + msg, self.name, *args)
