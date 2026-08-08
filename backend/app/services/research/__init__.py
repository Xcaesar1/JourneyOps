"""Provider-neutral travel research contracts and query generation."""

from .cache import MemoryResearchCache, NoopResearchCache, RedisResearchCache
from .contracts import ResearchCache, WebResearchProvider
from .providers import (
    BraveSearchProvider,
    FallbackWebResearchProvider,
    NoopWebResearchProvider,
    build_web_research_provider,
)
from .queries import prepare_research_queries

__all__ = [
    "BraveSearchProvider",
    "FallbackWebResearchProvider",
    "MemoryResearchCache",
    "NoopResearchCache",
    "NoopWebResearchProvider",
    "RedisResearchCache",
    "ResearchCache",
    "WebResearchProvider",
    "build_web_research_provider",
    "prepare_research_queries",
]
