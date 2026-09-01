"""Packaging contract for the daemonless nightly refresh schedule."""

from importlib.resources import files


def _unit(name: str) -> str:
    return (
        files("cc_search_chats").joinpath("systemd", name).read_text(encoding="utf-8")
    )


def test_nightly_refresh_is_a_low_priority_oneshot_service() -> None:
    service = _unit("cc-search-chats-index.service")

    assert "Type=oneshot" in service
    assert "ExecStart=%h/.local/bin/cc-search-chats index" in service
    assert "Environment=CC_SEARCH_CONTAINED=1" in service
    assert "Nice=10" in service
    assert "IOSchedulingClass=idle" in service
    assert "CPUWeight=25" in service
    assert "IOWeight=25" in service


def test_nightly_refresh_reads_only_explicit_operator_configuration() -> None:
    service = _unit("cc-search-chats-index.service")

    assert "EnvironmentFile=-%h/.config/cc-search-chats/index.env" in service
    assert "CC_SEARCH_CLAUDE_ROOT=" not in service
    assert "CC_SEARCH_CODEX_ROOT=" not in service
    assert "CC_SEARCH_CLAUDE_ROOTS=" not in service
    assert "CC_SEARCH_CODEX_ROOTS=" not in service
    assert "PGPASSWORD=" not in service
    assert "HF_HOME=" not in service
    assert "TORCH_HOME=" not in service
    assert "UV_CACHE_DIR=" not in service


def test_nightly_refresh_timer_is_persistent_and_dephased() -> None:
    timer = _unit("cc-search-chats-index.timer")

    assert "OnCalendar=*-*-* 03:00:00" in timer
    assert "RandomizedDelaySec=30m" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_automatic_refresh_is_a_separate_full_index_oneshot() -> None:
    service = _unit("cc-search-chats-refresh.service")

    assert "Type=oneshot" in service
    assert (
        "ExecStart=%h/.local/bin/cc-search-chats index --background-refresh" in service
    )
    assert "--literal-only" not in service
    assert "Environment=CC_SEARCH_CONTAINED=1" in service
    assert "Nice=10" in service
    assert "IOSchedulingClass=idle" in service
    assert "CPUWeight=25" in service
    assert "IOWeight=25" in service
    assert "[Install]" not in service
