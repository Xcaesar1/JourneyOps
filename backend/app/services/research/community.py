"""Optional Xiaohongshu community provider with failure isolation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ...config import get_settings
from ...domain.research_models import CommunityResearchResult

CommunitySearch = Callable[[str, str, str], str]


def _fallback_context(language: str) -> str:
    normalized = language.lower().split("-", 1)[0]
    if normalized == "zh":
        return "社区体验来源不可用；请仅使用地图、官方来源和通用建议。"
    if normalized == "ja":
        return "コミュニティ情報は利用できません。地図、公式情報、一般的な提案のみを使用してください。"
    return "Community insights are unavailable; use maps, official sources, and general guidance only."


class NoopCommunityResearchProvider:
    name = "community_noop"

    async def research(
        self,
        city: str,
        keywords: str,
        language: str,
    ) -> CommunityResearchResult:
        _ = city, keywords
        return CommunityResearchResult(
            provider=self.name,
            status="disabled",
            context=_fallback_context(language),
        )


class XhsCommunityResearchProvider:
    """Treat XHS as subjective context and redact every upstream failure."""

    name = "xiaohongshu"

    def __init__(self, *, search: CommunitySearch | None = None) -> None:
        self._search = search

    @staticmethod
    def _default_search(city: str, keywords: str, language: str) -> str:
        from ..xhs_service import search_xhs_attractions

        return search_xhs_attractions(city, keywords, language)

    async def research(
        self,
        city: str,
        keywords: str,
        language: str,
    ) -> CommunityResearchResult:
        search = self._search or self._default_search
        try:
            context = await asyncio.to_thread(search, city, keywords, language)
        except Exception:
            return CommunityResearchResult(
                provider=self.name,
                status="unavailable",
                context=_fallback_context(language),
                error_code="unavailable",
            )
        if not context.strip() or context.startswith("未在小红书检索到"):
            return CommunityResearchResult(
                provider=self.name,
                status="empty",
                context=_fallback_context(language),
                error_code="empty_results",
            )
        return CommunityResearchResult(
            provider=self.name,
            status="available",
            context=context,
        )


def build_community_research_provider():
    settings = get_settings()
    if not settings.xhs_enabled or not settings.xhs_cookie.strip():
        return NoopCommunityResearchProvider()
    return XhsCommunityResearchProvider()
