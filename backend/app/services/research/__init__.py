"""Provider-neutral travel research contracts and query generation."""

from .contracts import WebResearchProvider
from .queries import prepare_research_queries

__all__ = ["WebResearchProvider", "prepare_research_queries"]

