"""Optional community-provider behavior and redaction tests."""

from __future__ import annotations

from backend.app.services.research.community import (
    NoopCommunityResearchProvider,
    XhsCommunityResearchProvider,
)


async def test_disabled_community_provider_returns_safe_context() -> None:
    result = await NoopCommunityResearchProvider().research("Tokyo", "food", "en")

    assert result.status == "disabled"
    assert "official sources" in result.context


async def test_xhs_cookie_failure_does_not_escape_or_leak_error_text() -> None:
    def fail_with_secret(_city: str, _keywords: str, _language: str) -> str:
        raise RuntimeError("web_session=top-secret")

    result = await XhsCommunityResearchProvider(search=fail_with_secret).research(
        "Tokyo", "food", "en"
    )

    assert result.status == "unavailable"
    assert result.error_code == "unavailable"
    assert "top-secret" not in result.model_dump_json()


async def test_xhs_community_context_is_available_when_search_succeeds() -> None:
    provider = XhsCommunityResearchProvider(search=lambda *_args: "Community experience")

    result = await provider.research("Tokyo", "food", "en")

    assert result.status == "available"
    assert result.context == "Community experience"
