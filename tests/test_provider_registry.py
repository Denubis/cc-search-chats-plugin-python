"""Registry entries agree with the identity grammar and each adapter's surface."""

from pathlib import Path

import pytest

from cc_search_chats.core.identity import Provider, permitted_locator_key_kinds
from cc_search_chats.providers import registry
from cc_search_chats.providers.registry import (
    ProviderAdapter,
    configured_source_roots,
    provider_adapter,
    provider_adapters,
)

RECORD_POLICY_PARSER_STATE_VERSIONS = {
    Provider.CLAUDE: 7,
    Provider.CODEX: 5,
    Provider.ANTIGRAVITY: 1,
}
REGISTERED = list(Provider)


def test_every_provider_has_exactly_one_registered_adapter() -> None:
    adapters = provider_adapters()
    assert [adapter.provider for adapter in adapters] == REGISTERED
    assert all(isinstance(adapter, ProviderAdapter) for adapter in adapters)
    for adapter in adapters:
        assert provider_adapter(adapter.provider) is adapter


def test_unregistered_provider_lookup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(registry._ADAPTERS, Provider.ANTIGRAVITY)
    assert [adapter.provider for adapter in provider_adapters()] == [
        Provider.CLAUDE,
        Provider.CODEX,
    ]
    with pytest.raises(KeyError):
        provider_adapter(Provider.ANTIGRAVITY)


@pytest.mark.parametrize("provider", list(RECORD_POLICY_PARSER_STATE_VERSIONS))
def test_parser_state_version_matches_record_policy(provider: Provider) -> None:
    assert (
        provider_adapter(provider).parser_state_version
        == RECORD_POLICY_PARSER_STATE_VERSIONS[provider]
    )


@pytest.mark.parametrize("provider", REGISTERED)
def test_initial_state_round_trips_through_serialisation(provider: Provider) -> None:
    adapter = provider_adapter(provider)
    state = adapter.initial_state()
    serialized = adapter.serialize_state(state)
    assert isinstance(serialized, dict)
    assert adapter.deserialize_state(serialized) == state


@pytest.mark.parametrize("provider", REGISTERED)
def test_empty_parse_yields_initial_continuation_state(provider: Provider) -> None:
    adapter = provider_adapter(provider)
    parsed = adapter.parse(
        (),
        source_session_id=adapter.source_session_id(Path("session-1.jsonl")),
        source_diagnostics=(),
        prior_state=None,
    )
    assert parsed.messages == ()
    assert parsed.next_state == adapter.initial_state()


@pytest.mark.parametrize("provider", REGISTERED)
def test_locator_key_kinds_agree_with_identity_table(provider: Provider) -> None:
    assert provider_adapter(provider).locator_key_kinds == permitted_locator_key_kinds(
        provider
    )


@pytest.mark.parametrize("provider", REGISTERED)
def test_diagnostic_code_sets_are_disjoint(provider: Provider) -> None:
    adapter = provider_adapter(provider)
    assert not adapter.unsupported_codes & adapter.skippable_codes
    assert not adapter.unsupported_codes & adapter.repaired_codes
    assert not adapter.skippable_codes & adapter.repaired_codes


@pytest.mark.parametrize("provider", REGISTERED)
def test_discover_on_an_empty_root_finds_nothing(
    provider: Provider, tmp_path: Path
) -> None:
    discovery = provider_adapter(provider).discover(tmp_path, inspect_content=False)
    assert discovery.sources == ()
    assert discovery.diagnostics == ()


def test_default_roots_follow_registry_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    roots = configured_source_roots(environ={}, home=home)
    expected = [
        (adapter.provider, path)
        for adapter in provider_adapters()
        for path in adapter.default_roots(home.resolve())
    ]
    assert [(root.provider, root.path) for root in roots] == expected
