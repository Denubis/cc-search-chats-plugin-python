"""Pure, fail-closed adapter for native Codex JSONL records."""

# pattern: Functional Core

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeIs

from cc_search_chats.core.canonicalization import (
    CanonicalizationDiagnosticCode,
    CanonicalizationResult,
    CodexRecordFamily,
    PhysicalMessageCandidate,
    canonicalize_codex_candidates,
    codex_logical_message_id,
    is_valid_native_timestamp,
)
from cc_search_chats.core.identity import (
    ContentClass,
    LocatorKeyKind,
    MessageIdentity,
    NativeLocator,
    NativeMessage,
    PhysicalAlias,
    Provider,
    SessionEpochBoundary,
    SessionKind,
    SubmittedBy,
    is_unicode_scalar_text,
    repair_unstorable_text,
)
from cc_search_chats.providers.source_discovery import (
    RecordEnvelope,
    SourceDiagnostic,
    SourceDiagnosticCode,
)

_PRIMARY_SOURCES = {"cli", "exec", "mcp", "vscode"}
_LEGACY_SUBAGENT_ROLES = {"review"}
_TURN_CONTEXT_REQUIRED_FIELDS = {
    "approval_policy",
    "cwd",
    "model",
    "sandbox_policy",
    "summary",
}
_TURN_CONTEXT_STRING_FIELDS = {
    "approval_policy",
    "approvals_reviewer",
    "comp_hash",
    "current_date",
    "cwd",
    "effort",
    "model",
    "multi_agent_mode",
    "multi_agent_version",
    "personality",
    "summary",
    "timezone",
    "turn_id",
    "user_instructions",
}
_TURN_CONTEXT_OBJECT_FIELDS = {
    "collaboration_mode",
    "file_system_sandbox_policy",
    "permission_profile",
    "sandbox_policy",
    "truncation_policy",
}
_TURN_CONTEXT_BOOLEAN_FIELDS = {"realtime_active"}
_TURN_CONTEXT_LIST_FIELDS = {"workspace_roots"}
_TURN_CONTEXT_FIELDS = (
    _TURN_CONTEXT_STRING_FIELDS
    | _TURN_CONTEXT_OBJECT_FIELDS
    | _TURN_CONTEXT_BOOLEAN_FIELDS
    | _TURN_CONTEXT_LIST_FIELDS
)
_TOKEN_USAGE_REQUIRED_COUNTERS = {
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
}
_TOKEN_USAGE_ID_FIELDS = {
    "thread_id",
    "turn_id",
    "session_id",
    "root_turn_id",
    "response_id",
}
_TOKEN_USAGE_FIELDS = {"usage", "turn_token_usage", "thread_token_usage"}

_LIFECYCLE_EVENT_KEYSETS = {
    "task_started": {
        frozenset(
            {
                "type",
                "collaboration_mode_kind",
                "model_context_window",
                "started_at",
                "turn_id",
            }
        ),
        frozenset(
            {
                "type",
                "collaboration_mode_kind",
                "model_context_window",
                "turn_id",
            }
        ),
    },
    "entered_review_mode": {frozenset({"type", "target", "user_facing_hint"})},
    "token_count": {frozenset({"type", "info", "rate_limits"})},
    "patch_apply_end": {
        frozenset(
            {
                "type",
                "call_id",
                "changes",
                "status",
                "stderr",
                "stdout",
                "success",
                "turn_id",
            }
        )
    },
    "task_complete": {
        frozenset({"type", "last_agent_message", "turn_id"}),
        frozenset(
            {
                "type",
                "completed_at",
                "duration_ms",
                "last_agent_message",
                "time_to_first_token_ms",
                "turn_id",
            }
        ),
        frozenset(
            {
                "type",
                "completed_at",
                "duration_ms",
                "error",
                "last_agent_message",
                "started_at",
                "time_to_first_token_ms",
                "turn_id",
            }
        ),
        frozenset(
            {
                "type",
                "completed_at",
                "duration_ms",
                "last_agent_message",
                "started_at",
                "time_to_first_token_ms",
                "turn_id",
            }
        ),
        frozenset(
            {
                "type",
                "completed_at",
                "duration_ms",
                "last_agent_message",
                "turn_id",
            }
        ),
    },
    "sub_agent_activity": {
        frozenset(
            {
                "type",
                "agent_path",
                "agent_thread_id",
                "event_id",
                "kind",
                "occurred_at_ms",
            }
        )
    },
    "item_completed": {
        frozenset(
            {
                "type",
                "completed_at_ms",
                "item",
                "started_at_ms",
                "thread_id",
                "turn_id",
            }
        )
    },
    "exec_command_end": {
        frozenset(
            {
                "type",
                "aggregated_output",
                "call_id",
                "command",
                "cwd",
                "duration",
                "exit_code",
                "formatted_output",
                "parsed_cmd",
                "process_id",
                "source",
                "status",
                "stderr",
                "stdout",
                "turn_id",
            }
        )
    },
    "turn_aborted": {
        frozenset({"type", "reason"}),
        frozenset({"type", "reason", "turn_id"}),
        frozenset({"type", "completed_at", "duration_ms", "reason", "turn_id"}),
        frozenset(
            {"type", "completed_at", "duration_ms", "reason", "started_at", "turn_id"}
        ),
    },
    "mcp_tool_call_end": {
        frozenset({"type", "call_id", "duration", "invocation", "result"}),
        frozenset(
            {
                "type",
                "action_name",
                "app_name",
                "call_id",
                "connector_id",
                "duration",
                "invocation",
                "link_id",
                "result",
            }
        ),
    },
    "web_search_end": {
        frozenset({"type", "action", "call_id", "query"}),
        frozenset({"type", "action", "call_id", "query", "results"}),
    },
    "context_compacted": {frozenset({"type"})},
    "agent_reasoning": {frozenset({"type", "text"})},
    "exited_review_mode": {frozenset({"type", "review_output"})},
}

_LIFECYCLE_EVENT_KEYSETS["mcp_tool_call_end"].update(
    {
        frozenset(
            {"type", "call_id", "duration", "invocation", "read_only_hint", "result"}
        ),
        frozenset(
            {
                "type",
                "call_id",
                "connector_id",
                "duration",
                "invocation",
                "link_id",
                "result",
            }
        ),
        frozenset(
            {
                "type",
                "action_name",
                "app_name",
                "call_id",
                "connector_id",
                "duration",
                "invocation",
                "link_id",
                "read_only_hint",
                "result",
            }
        ),
    }
)
_LIFECYCLE_EVENT_KEYSETS["task_complete"].update(
    {
        frozenset(
            {
                "type",
                "completed_at",
                "duration_ms",
                "error",
                "last_agent_message",
                "started_at",
                "turn_id",
            }
        ),
        frozenset(
            {
                "type",
                "completed_at",
                "duration_ms",
                "last_agent_message",
                "started_at",
                "turn_id",
            }
        ),
    }
)
_LIFECYCLE_EVENT_KEYSETS.update(
    {
        "collab_agent_spawn_end": {
            frozenset(
                {
                    "type",
                    "call_id",
                    "model",
                    "new_agent_nickname",
                    "new_thread_id",
                    "prompt",
                    "reasoning_effort",
                    "sender_thread_id",
                    "status",
                }
            ),
            frozenset(
                {
                    "type",
                    "call_id",
                    "model",
                    "new_agent_nickname",
                    "new_agent_role",
                    "new_thread_id",
                    "prompt",
                    "reasoning_effort",
                    "sender_thread_id",
                    "status",
                }
            ),
        },
        "collab_close_end": {
            frozenset(
                {
                    "type",
                    "call_id",
                    "receiver_agent_nickname",
                    "receiver_agent_role",
                    "receiver_thread_id",
                    "sender_thread_id",
                    "status",
                }
            )
        },
        "collab_waiting_end": {
            frozenset({"type", "call_id", "sender_thread_id", "statuses"})
        },
        "error": {frozenset({"type", "codex_error_info", "message"})},
        "thread_goal_updated": {frozenset({"type", "goal", "threadId"})},
        "thread_rolled_back": {frozenset({"type", "num_turns"})},
        "thread_settings_applied": {frozenset({"type", "thread_settings"})},
    }
)

_INTER_AGENT_RESPONSE_KEYSETS = {
    "agent_message": {
        frozenset(
            {
                "type",
                "author",
                "content",
                "internal_chat_message_metadata_passthrough",
                "recipient",
            }
        ),
        frozenset(
            {
                "type",
                "author",
                "content",
                "id",
                "internal_chat_message_metadata_passthrough",
                "recipient",
            }
        ),
    },
    "tool_search_call": {
        frozenset(
            {
                "type",
                "arguments",
                "call_id",
                "execution",
                "id",
                "internal_chat_message_metadata_passthrough",
                "status",
            }
        ),
        frozenset({"type", "arguments", "call_id", "execution", "status"}),
    },
    "web_search_call": {frozenset({"type", "action", "status"})},
}

_INTER_AGENT_RESPONSE_KEYSETS["web_search_call"].update(
    {
        frozenset(
            {
                "type",
                "action",
                "id",
                "internal_chat_message_metadata_passthrough",
                "status",
            }
        ),
        frozenset({"type", "status"}),
    }
)
_INTER_AGENT_RESPONSE_KEYSETS["tool_search_output"] = {
    frozenset(
        {
            "type",
            "call_id",
            "execution",
            "internal_chat_message_metadata_passthrough",
            "status",
            "tools",
        }
    ),
    frozenset({"type", "call_id", "execution", "status", "tools"}),
}


class CodexDiagnosticCode(StrEnum):
    """Closed Codex parse and exclusion classifications."""

    MALFORMED_JSON = "malformed_json"
    INVALID_ENCODING = "invalid_encoding"
    UNSUPPORTED_SOURCE_SHAPE = "unsupported_source_shape"
    EXCLUDED_DEVELOPER = "excluded_developer"
    EXCLUDED_INJECTED = "excluded_injected"
    UNKNOWN_ROLE = "unknown_role"
    UNKNOWN_CONTENT_BLOCK = "unknown_content_block"
    EXCLUDED_REASONING = "excluded_reasoning"
    EXCLUDED_TOOL_RESULT = "excluded_tool_result"
    UNKNOWN_RESPONSE_ITEM = "unknown_response_item"
    UNKNOWN_EVENT = "unknown_event"
    UNKNOWN_OUTER_TYPE = "unknown_outer_type"
    UNKNOWN_COMPACTION = "unknown_compaction"
    DUPLICATE_COMPACTION = "duplicate_compaction"
    EXCLUDED_INTER_AGENT_METADATA = "excluded_inter_agent_metadata"
    EXCLUDED_NON_TEXT_CONTENT = "excluded_non_text_content"
    EXCLUDED_LIFECYCLE_EVENT = "excluded_lifecycle_event"
    EXCLUDED_METADATA = "excluded_metadata"
    PARTIAL_TAIL = "partial_tail"
    INVALID_PAYLOAD = "invalid_payload"
    REPAIRED_UNICODE = "repaired_unicode"
    INVALID_UNICODE = "invalid_unicode"
    EMPTY_CONTENT = "empty_content"
    UNKNOWN_METADATA_SHAPE = "unknown_metadata_shape"
    UNSUPPORTED_SESSION_IDENTITY = "unsupported_session_identity"


@dataclass(frozen=True, slots=True)
class CodexDiagnostic:
    """One named Codex outcome with optional physical coordinates."""

    code: CodexDiagnosticCode
    detail: str
    record_ordinal: int | None
    source_line: int | None
    source_byte_offset: int | None


@dataclass(frozen=True, slots=True)
class CodexSessionContext:
    """Caller context plus an optional expected native session identity.

    ``source_session_id`` is retained for compatibility with Phase 1 callers,
    but it is validation evidence only. Native metadata or prior parser state
    owns the identity used to construct locators.
    """

    source_session_id: str | None = None
    repository: str | None = None
    cwd: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed expected identities at the caller boundary."""
        if (
            self.source_session_id is not None
            and _locator_safe_id(self.source_session_id) is None
        ):
            raise ValueError("source_session_id must be a nonempty locator-safe string")


@dataclass(frozen=True, slots=True)
class CodexParserState:
    """Immutable Codex state required to parse only an appended suffix."""

    next_conversation_epoch: int = 0
    session_kind: SessionKind | None = None
    seen_compaction_digests: tuple[str, ...] = ()
    trailing_candidate: PhysicalMessageCandidate | None = None
    source_session_id: str | None = None

    def __post_init__(self) -> None:
        """Reject malformed or cross-epoch persisted parser state."""
        if (
            isinstance(self.next_conversation_epoch, bool)
            or not isinstance(self.next_conversation_epoch, int)
            or self.next_conversation_epoch < 0
        ):
            raise ValueError("next_conversation_epoch must be a nonnegative integer")
        if self.session_kind is not None and not isinstance(
            self.session_kind, SessionKind
        ):
            raise ValueError("session_kind must be a SessionKind or None")
        if (
            self.source_session_id is not None
            and _locator_safe_id(self.source_session_id) is None
        ):
            raise ValueError(
                "source_session_id must be a nonempty locator-safe string or None"
            )
        if (self.session_kind is None) != (self.source_session_id is None):
            raise ValueError(
                "source_session_id and session_kind must be established together"
            )
        if not isinstance(self.seen_compaction_digests, tuple) or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or not set(digest) <= set("0123456789abcdef")
            for digest in self.seen_compaction_digests
        ):
            raise ValueError("seen_compaction_digests must contain SHA-256 hex strings")
        if len(set(self.seen_compaction_digests)) != len(self.seen_compaction_digests):
            raise ValueError("seen_compaction_digests must be unique")
        candidate = self.trailing_candidate
        if candidate is None:
            return
        if not isinstance(candidate, PhysicalMessageCandidate):
            raise ValueError(  # noqa: TRY004  # parser-state ValueError contract
                "trailing_candidate must be a physical message candidate"
            )
        message = candidate.message
        if (
            candidate.record_family
            not in {
                CodexRecordFamily.RESPONSE_MESSAGE,
                CodexRecordFamily.EVENT_MESSAGE,
            }
            or message.content_class is not ContentClass.PROSE
            or message.conversation_epoch != self.next_conversation_epoch
            or len(message.identity.physical_aliases) != 1
            or message.identity.canonical_locator.provider is not Provider.CODEX
            or self.session_kind is None
            or message.identity.canonical_locator.source_session_id
            != self.source_session_id
            or message.session_kind is not self.session_kind
        ):
            raise ValueError(
                "trailing_candidate must be one unpaired Codex prose projection "
                "in the continuation epoch and session kind"
            )


@dataclass(frozen=True, slots=True)
class CodexParseResult:
    """Canonical messages plus retained physical and diagnostic outcomes."""

    source_session_id: str | None
    session_kind: SessionKind
    messages: tuple[NativeMessage, ...]
    boundaries: tuple[SessionEpochBoundary, ...]
    diagnostics: tuple[CodexDiagnostic, ...]
    next_state: CodexParserState


@dataclass(frozen=True, slots=True)
class _DecodedRecord:
    envelope: RecordEnvelope
    payload: dict[str, object]


def _is_json_object(value: object) -> TypeIs[dict[str, object]]:
    """Narrow an untrusted JSON value after checking its key contract."""
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _diagnostic(
    code: CodexDiagnosticCode, envelope: RecordEnvelope, detail: str
) -> CodexDiagnostic:
    """Attach a named Codex outcome to Task 2 source coordinates."""
    return CodexDiagnostic(
        code=code,
        detail=detail,
        record_ordinal=envelope.record_ordinal,
        source_line=envelope.source_line,
        source_byte_offset=envelope.source_byte_offset,
    )


def _decode_records(
    envelopes: tuple[RecordEnvelope, ...],
) -> tuple[tuple[_DecodedRecord, ...], tuple[CodexDiagnostic, ...]]:
    """Decode complete records without admitting non-object JSON."""
    records: list[_DecodedRecord] = []
    diagnostics: list[CodexDiagnostic] = []
    for envelope in envelopes:
        try:
            payload: object = json.loads(envelope.raw_bytes)
        except UnicodeDecodeError:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.INVALID_ENCODING,
                    envelope,
                    "complete record is not valid UTF-8",
                )
            )
            continue
        except json.JSONDecodeError:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.MALFORMED_JSON,
                    envelope,
                    "complete record is not valid JSON",
                )
            )
            continue
        if not _is_json_object(payload):
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
                    envelope,
                    "top-level JSON value is not an object",
                )
            )
            continue
        records.append(_DecodedRecord(envelope=envelope, payload=payload))
    return tuple(records), tuple(diagnostics)


def _modern_subagent(source: object) -> bool:
    """Recognize the modern source.subagent.thread_spawn lineage shape."""
    if not _is_json_object(source):
        return False
    subagent = source.get("subagent")
    if not _is_json_object(subagent):
        return False
    spawn = subagent.get("thread_spawn")
    if not _is_json_object(spawn):
        return False
    parent = spawn.get("parent_thread_id")
    depth = spawn.get("depth")
    return (
        isinstance(parent, str)
        and bool(parent)
        and not isinstance(depth, bool)
        and isinstance(depth, int)
        and depth >= 1
    )


def _legacy_subagent(source: object) -> bool:
    """Recognize the legacy source.subagent role string."""
    if not _is_json_object(source):
        return False
    subagent = source.get("subagent")
    return isinstance(subagent, str) and subagent in _LEGACY_SUBAGENT_ROLES


def _source_kind(source: object) -> SessionKind | None:
    """Classify only audited primary and child source shapes."""
    if isinstance(source, str) and source in _PRIMARY_SOURCES:
        return SessionKind.PRIMARY
    if _modern_subagent(source) or _legacy_subagent(source):
        return SessionKind.AGENT
    return None


def _child_lineage_identity(payload: dict[str, object]) -> str | None:
    """Resolve the audited child-thread identity without treating its parent as it."""
    source = payload.get("source")
    if not _modern_subagent(source) or not _is_json_object(source):
        return None
    subagent = source.get("subagent")
    if not _is_json_object(subagent):
        return None
    spawn = subagent.get("thread_spawn")
    if not _is_json_object(spawn):
        return None
    identity = _locator_safe_id(payload.get("id"))
    spawn_parent = _locator_safe_id(spawn.get("parent_thread_id"))
    if (
        payload.get("thread_source") in {None, "subagent"}
        and identity is not None
        and spawn_parent is not None
        and identity != spawn_parent
    ):
        return identity
    return None


def _session_kind(
    records: tuple[_DecodedRecord, ...],
) -> tuple[SessionKind, tuple[CodexDiagnostic, ...]]:
    """Classify the owning, first session_meta without reclassifying copied history."""
    owner = next(
        (record for record in records if record.payload.get("type") == "session_meta"),
        None,
    )
    if owner is None:
        return SessionKind.UNKNOWN, ()
    meta = owner.payload.get("payload")
    if not _is_json_object(meta):
        return SessionKind.UNKNOWN, (
            _diagnostic(
                CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
                owner.envelope,
                "session_meta payload is not an object",
            ),
        )
    kind = _source_kind(meta.get("source"))
    if kind is None:
        return SessionKind.UNKNOWN, (
            _diagnostic(
                CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
                owner.envelope,
                "session_meta source is unsupported",
            ),
        )
    return kind, ()


def _continued_session_kind(
    records: tuple[_DecodedRecord, ...],
    established: SessionKind | None,
) -> tuple[SessionKind, tuple[CodexDiagnostic, ...]]:
    """Use an established kind when a suffix omits session metadata."""
    if established is None:
        return _session_kind(records)
    metadata = tuple(
        record for record in records if record.payload.get("type") == "session_meta"
    )
    if not metadata:
        return established, ()
    observed, diagnostics = _session_kind(metadata)
    if observed is established and not diagnostics:
        return established, ()
    if diagnostics:
        return SessionKind.UNKNOWN, diagnostics
    if established is SessionKind.UNKNOWN:
        return observed, ()
    return SessionKind.UNKNOWN, (
        _diagnostic(
            CodexDiagnosticCode.UNSUPPORTED_SOURCE_SHAPE,
            metadata[0].envelope,
            "session_meta changes the established continuation session kind",
        ),
    )


def _session_identity_diagnostic(
    records: tuple[_DecodedRecord, ...], detail: str
) -> CodexDiagnostic:
    """Build an identity diagnostic even when no record decoded successfully."""
    if records:
        return _diagnostic(
            CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
            records[0].envelope,
            detail,
        )
    return CodexDiagnostic(
        code=CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
        detail=detail,
        record_ordinal=None,
        source_line=None,
        source_byte_offset=None,
    )


def _metadata_session_identity(
    records: tuple[_DecodedRecord, ...],
) -> tuple[str | None, bool, tuple[CodexDiagnostic, ...]]:
    """Resolve the owning metadata identity and validate copied lineage metadata."""
    metadata = tuple(
        record for record in records if record.payload.get("type") == "session_meta"
    )
    if not metadata:
        return None, False, ()

    def metadata_identity(payload: dict[str, object]) -> str | None:
        if _modern_subagent(payload.get("source")):
            return _child_lineage_identity(payload)
        present = tuple(payload[key] for key in ("id", "session_id") if key in payload)
        validated = tuple(_locator_safe_id(value) for value in present)
        if (
            not present
            or any(value is None for value in validated)
            or len(set(validated)) != 1
        ):
            return None
        return next(value for value in validated if value is not None)

    owner_record = metadata[0]
    owner_payload = owner_record.payload.get("payload")
    if not _is_json_object(owner_payload):
        return (
            None,
            True,
            (
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
                    owner_record.envelope,
                    "session_meta payload cannot establish a native session identity",
                ),
            ),
        )
    owner_identity = metadata_identity(owner_payload)
    if owner_identity is None:
        return (
            None,
            True,
            (
                _diagnostic(
                    CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
                    owner_record.envelope,
                    "session_meta id/session_id is missing, malformed, or conflicting",
                ),
            ),
        )

    def lineage_identities(payload: dict[str, object]) -> set[str]:
        identities = {
            value
            for key in ("forked_from_id", "parent_thread_id", "session_id")
            if (value := _locator_safe_id(payload.get(key))) is not None
        }
        source = payload.get("source")
        if not _is_json_object(source):
            return identities
        subagent = source.get("subagent")
        if not _is_json_object(subagent):
            return identities
        spawn = subagent.get("thread_spawn")
        if not _is_json_object(spawn):
            return identities
        spawn_parent = _locator_safe_id(spawn.get("parent_thread_id"))
        if spawn_parent is not None:
            identities.add(spawn_parent)
        return identities

    allowed_identities = {owner_identity, *lineage_identities(owner_payload)}
    diagnostics: list[CodexDiagnostic] = []
    for record in metadata[1:]:
        payload = record.payload.get("payload")
        observed = metadata_identity(payload) if _is_json_object(payload) else None
        if observed is None:
            detail = "session_meta payload cannot establish a native session identity"
        elif observed not in allowed_identities:
            detail = (
                "session_meta record is not the owner or an attested lineage identity"
            )
        else:
            if _is_json_object(payload):
                allowed_identities.update(lineage_identities(payload))
            continue
        diagnostics.append(
            _diagnostic(
                CodexDiagnosticCode.UNSUPPORTED_SESSION_IDENTITY,
                record.envelope,
                detail,
            )
        )
    if diagnostics:
        return None, True, tuple(diagnostics)
    return owner_identity, True, ()


def _resolved_session_identity(
    records: tuple[_DecodedRecord, ...],
    context: CodexSessionContext,
    established: str | None,
) -> tuple[str | None, tuple[CodexDiagnostic, ...]]:
    """Resolve identity only from native metadata or immutable prior state."""
    observed, metadata_present, diagnostics = _metadata_session_identity(records)
    if diagnostics:
        return None, diagnostics

    if established is None:
        if not metadata_present or observed is None:
            return None, (
                _session_identity_diagnostic(
                    records,
                    "fresh Codex input lacks session_meta native identity",
                ),
            )
        resolved = observed
    else:
        if metadata_present and observed != established:
            return None, (
                _session_identity_diagnostic(
                    records,
                    "session_meta changes the established native session identity",
                ),
            )
        resolved = established

    expected = context.source_session_id
    if expected is not None and expected != resolved:
        return None, (
            _session_identity_diagnostic(
                records,
                "caller expected session identity does not match native parser state",
            ),
        )
    return resolved, ()


def _locator_safe_id(value: object) -> str | None:
    """Return an opaque native ID only when it is locator-safe."""
    if not isinstance(value, str) or not value.strip():
        return None
    if any(delimiter in value for delimiter in (":", "\r", "\n")):
        return None
    return value


def _physical_alias(
    envelope: RecordEnvelope,
    source_session_id: str,
    native_id: str | None = None,
) -> PhysicalAlias:
    """Construct an ID locator or the exact ordinal/digest fallback."""
    if native_id is None:
        locator = NativeLocator(
            provider=Provider.CODEX,
            source_session_id=source_session_id,
            key_kind=LocatorKeyKind.ORDINAL,
            key=envelope.record_ordinal,
            record_digest=envelope.source_digest,
        )
    else:
        locator = NativeLocator(
            provider=Provider.CODEX,
            source_session_id=source_session_id,
            key_kind=LocatorKeyKind.ID,
            key=native_id,
        )
    return PhysicalAlias(
        locator=locator,
        source_file_relative=envelope.source_file_relative,
        record_ordinal=envelope.record_ordinal,
        source_line=envelope.source_line,
        source_byte_offset=envelope.source_byte_offset,
        raw_byte_length=envelope.raw_byte_length,
        source_digest=envelope.source_digest,
    )


def _native_message(
    *,
    record: _DecodedRecord,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
    role: str,
    content_class: ContentClass,
    text: str,
    family: CodexRecordFamily,
    native_id: str | None = None,
) -> PhysicalMessageCandidate:
    """Construct one common physical message/content candidate."""
    alias = _physical_alias(record.envelope, source_session_id, native_id)
    identified_agent = role == "user" and session_kind is SessionKind.AGENT
    timestamp = record.payload.get("timestamp")
    return PhysicalMessageCandidate(
        message=NativeMessage(
            identity=MessageIdentity(
                logical_message_id=codex_logical_message_id(alias),
                canonical_locator=alias.locator,
                physical_aliases=(alias,),
            ),
            timestamp=timestamp if isinstance(timestamp, str) else "",
            role=role,
            session_kind=session_kind,
            conversation_epoch=epoch,
            content_class=content_class,
            text=text,
            repository=context.repository,
            cwd=context.cwd,
            submitted_by=(
                SubmittedBy.IDENTIFIED_HARNESS
                if identified_agent
                else SubmittedBy.UNKNOWN
            ),
            submission_evidence=("codex:session_meta.source.subagent",)
            if identified_agent
            else (),
            submission_match_cardinality=1 if identified_agent else 0,
        ),
        record_family=family,
    )


def _is_injected_user_element(value: str) -> bool:
    """Recognize only the two ruled whole-block Codex harness wrappers."""
    trimmed = value.strip()
    for tag in ("environment_context", "user_instructions"):
        opening = f"<{tag}>"
        closing = f"</{tag}>"
        if trimmed.startswith(opening) and trimmed.endswith(closing):
            inner = trimmed[len(opening) : -len(closing)]
            return opening not in inner and closing not in inner
    return False


def _input_image_shape(block: dict[str, object]) -> bool:
    """Recognize images with the legacy optional detail and additive metadata."""
    return (
        block.get("type") == "input_image"
        and isinstance(block.get("image_url"), str)
        and ("detail" not in block or isinstance(block["detail"], str))
    )


def _extract_message_text_block(
    block: object,
    role: str,
    envelope: RecordEnvelope,
) -> tuple[str | None, bool, bool, tuple[CodexDiagnostic, ...]]:
    """Classify one Codex message block and return its visible text, if any."""
    if not _is_json_object(block):
        return (
            None,
            False,
            False,
            (
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                    envelope,
                    "message content block is not an object",
                ),
            ),
        )
    block_type = block.get("type")
    if block_type in {"reasoning", "summary_text"}:
        return (
            None,
            False,
            False,
            (
                _diagnostic(
                    CodexDiagnosticCode.EXCLUDED_REASONING,
                    envelope,
                    "reasoning content is deliberately non-searchable",
                ),
            ),
        )
    if _input_image_shape(block):
        return (
            None,
            False,
            False,
            (
                _diagnostic(
                    CodexDiagnosticCode.EXCLUDED_NON_TEXT_CONTENT,
                    envelope,
                    "image content is deliberately non-searchable",
                ),
            ),
        )
    expected = "input_text" if role == "user" else "output_text"
    text = block.get("text")
    if block_type != expected or not isinstance(text, str):
        return (
            None,
            False,
            False,
            (
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                    envelope,
                    f"unrecognized or role-incompatible message block: {block_type!r}",
                ),
            ),
        )
    if role == "user" and _is_injected_user_element(text):
        return None, True, False, ()
    text, repaired = repair_unstorable_text(text)
    if not is_unicode_scalar_text(text):
        return (
            None,
            False,
            True,
            (
                _diagnostic(
                    CodexDiagnosticCode.INVALID_UNICODE,
                    envelope,
                    "message text contains a non-scalar Unicode value",
                ),
            ),
        )
    diagnostics = (
        (
            _diagnostic(
                CodexDiagnosticCode.REPAIRED_UNICODE,
                envelope,
                "message text contained unstorable code points repaired with U+FFFD",
            ),
        )
        if repaired
        else ()
    )
    return text, False, False, diagnostics


def _message_content(
    content: object,
    role: str,
    envelope: RecordEnvelope,
) -> tuple[str | None, tuple[CodexDiagnostic, ...]]:
    """Extract only role-compatible visible Codex message blocks."""
    if not isinstance(content, list):
        return None, (
            _diagnostic(
                CodexDiagnosticCode.UNKNOWN_CONTENT_BLOCK,
                envelope,
                "message content is not a block list",
            ),
        )
    if not content:
        return None, (
            _diagnostic(
                CodexDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized message has an empty content block list",
            ),
        )
    text_parts: list[str] = []
    diagnostics: list[CodexDiagnostic] = []
    invalid_unicode = False
    removed_injected = False
    for block in content:
        text, removed, invalid, block_diagnostics = _extract_message_text_block(
            block, role, envelope
        )
        diagnostics.extend(block_diagnostics)
        removed_injected = removed_injected or removed
        invalid_unicode = invalid_unicode or invalid
        if text is not None:
            text_parts.append(text)
    if invalid_unicode:
        return None, tuple(diagnostics)
    if removed_injected and not any(text_parts):
        diagnostics.append(
            _diagnostic(
                CodexDiagnosticCode.EXCLUDED_INJECTED,
                envelope,
                "injected user context is deliberately non-searchable",
            )
        )
        return None, tuple(diagnostics)
    if not text_parts:
        return None, tuple(diagnostics)
    if not any(text_parts):
        diagnostics.append(
            _diagnostic(
                CodexDiagnosticCode.EMPTY_CONTENT,
                envelope,
                "recognized message has empty typed prose",
            )
        )
        return None, tuple(diagnostics)
    return "\n".join(text_parts), tuple(diagnostics)


def _parse_tool_call_item(
    *,
    item_type: object,
    payload: dict[str, object],
    record: _DecodedRecord,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
) -> tuple[tuple[PhysicalMessageCandidate, ...], tuple[CodexDiagnostic, ...]]:
    """Parse one ruled Codex function/custom tool invocation."""
    name = payload.get("name")
    tool_input = _tool_text(
        payload.get("arguments")
        if item_type == "function_call"
        else payload.get("input")
    )
    repaired = False
    if isinstance(name, str):
        name, repaired_name = repair_unstorable_text(name)
        repaired = repaired or repaired_name
    if tool_input is not None:
        tool_input, repaired_input = repair_unstorable_text(tool_input)
        repaired = repaired or repaired_input
    if (isinstance(name, str) and not is_unicode_scalar_text(name)) or (
        tool_input is not None and not is_unicode_scalar_text(tool_input)
    ):
        return (), (
            _diagnostic(
                CodexDiagnosticCode.INVALID_UNICODE,
                record.envelope,
                "tool call contains a non-scalar Unicode value",
            ),
        )
    if not isinstance(name, str) or not name or tool_input is None:
        return (), (
            _diagnostic(
                CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
                record.envelope,
                "tool call lacks a recognized name/input shape",
            ),
        )
    messages = tuple(
        _native_message(
            record=record,
            context=context,
            source_session_id=source_session_id,
            session_kind=session_kind,
            epoch=epoch,
            role="assistant",
            content_class=content_class,
            text=text,
            family=CodexRecordFamily.TOOL,
        )
        for content_class, text in (
            (ContentClass.TOOL_NAME, name),
            (ContentClass.TOOL_INPUT, tool_input),
        )
    )
    diagnostics = (
        (
            _diagnostic(
                CodexDiagnosticCode.REPAIRED_UNICODE,
                record.envelope,
                "tool call contained unstorable code points repaired with U+FFFD",
            ),
        )
        if repaired
        else ()
    )
    return messages, diagnostics


def _tool_text(value: object) -> str | None:
    """Render recognized tool values without flattening unknown objects."""
    if isinstance(value, str):
        return value
    if value is None:
        return None
    if _is_json_object(value) or isinstance(value, list):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return None


def _matches_excluded_keyset(
    payload: dict[str, object], required_keysets: set[frozenset[str]]
) -> bool:
    """Recognize excluded metadata when every required key is present."""
    payload_keys = frozenset(payload)
    return any(required_keys <= payload_keys for required_keys in required_keysets)


def _ghost_snapshot_shape(payload: dict[str, object]) -> bool:
    """Recognize an audited ghost commit without exposing its filesystem metadata."""
    commit = payload.get("ghost_commit")
    return (
        payload.get("type") == "ghost_snapshot"
        and _is_json_object(commit)
        and set(commit)
        >= {"id", "parent", "preexisting_untracked_files", "preexisting_untracked_dirs"}
        and isinstance(commit["id"], str)
        and (commit["parent"] is None or isinstance(commit["parent"], str))
        and _is_string_list(commit["preexisting_untracked_files"])
        and _is_string_list(commit["preexisting_untracked_dirs"])
    )


def _parse_response_item(
    record: _DecodedRecord,
    *,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
) -> tuple[tuple[PhysicalMessageCandidate, ...], tuple[CodexDiagnostic, ...]]:
    """Parse one allowlisted response_item capability shape."""
    payload = record.payload.get("payload")
    if not _is_json_object(payload):
        return (), (
            _diagnostic(
                CodexDiagnosticCode.INVALID_PAYLOAD,
                record.envelope,
                "response_item payload is not an object",
            ),
        )
    item_type = payload.get("type")
    if _ghost_snapshot_shape(payload):
        return (), (
            _diagnostic(
                CodexDiagnosticCode.EXCLUDED_METADATA,
                record.envelope,
                "ghost snapshot is deliberately non-searchable",
            ),
        )
    if item_type == "reasoning":
        return (), (
            _diagnostic(
                CodexDiagnosticCode.EXCLUDED_REASONING,
                record.envelope,
                "reasoning item is deliberately non-searchable",
            ),
        )
    if isinstance(item_type, str) and _matches_excluded_keyset(
        payload, _INTER_AGENT_RESPONSE_KEYSETS.get(item_type, set())
    ):
        return (), (
            _diagnostic(
                CodexDiagnosticCode.EXCLUDED_INTER_AGENT_METADATA,
                record.envelope,
                f"{item_type} item is deliberately non-searchable",
            ),
        )
    if item_type == "message":
        role = payload.get("role")
        if role == "developer":
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.EXCLUDED_DEVELOPER,
                    record.envelope,
                    "developer message is deliberately non-searchable",
                ),
            )
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            return (), (
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_ROLE,
                    record.envelope,
                    f"unrecognized Codex message role: {role!r}",
                ),
            )
        text, diagnostics = _message_content(
            payload.get("content"), role, record.envelope
        )
        if text is None:
            return (), diagnostics
        native_id = _locator_safe_id(payload.get("id"))
        return (
            (
                _native_message(
                    record=record,
                    context=context,
                    source_session_id=source_session_id,
                    session_kind=session_kind,
                    epoch=epoch,
                    role=role,
                    content_class=ContentClass.PROSE,
                    text=text,
                    family=CodexRecordFamily.RESPONSE_MESSAGE,
                    native_id=native_id,
                ),
            ),
            diagnostics,
        )
    if item_type in {"function_call", "custom_tool_call"}:
        return _parse_tool_call_item(
            item_type=item_type,
            payload=payload,
            record=record,
            context=context,
            source_session_id=source_session_id,
            session_kind=session_kind,
            epoch=epoch,
        )
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        return (), (
            _diagnostic(
                CodexDiagnosticCode.EXCLUDED_TOOL_RESULT,
                record.envelope,
                "tool result content is deliberately non-searchable",
            ),
        )
    return (), (
        _diagnostic(
            CodexDiagnosticCode.UNKNOWN_RESPONSE_ITEM,
            record.envelope,
            f"unrecognized response_item payload type: {item_type!r}",
        ),
    )


def _parse_event(
    record: _DecodedRecord,
    *,
    context: CodexSessionContext,
    source_session_id: str,
    session_kind: SessionKind,
    epoch: int,
) -> tuple[PhysicalMessageCandidate | None, CodexDiagnostic | None]:
    """Parse only visible user_message and agent_message event projections."""
    payload = record.payload.get("payload")
    if not _is_json_object(payload):
        return None, _diagnostic(
            CodexDiagnosticCode.INVALID_PAYLOAD,
            record.envelope,
            "event_msg payload is not an object",
        )
    event_type = payload.get("type")
    if isinstance(event_type, str) and _matches_excluded_keyset(
        payload, _LIFECYCLE_EVENT_KEYSETS.get(event_type, set())
    ):
        return None, _diagnostic(
            CodexDiagnosticCode.EXCLUDED_LIFECYCLE_EVENT,
            record.envelope,
            f"{event_type} lifecycle event is deliberately non-searchable",
        )
    role = {"user_message": "user", "agent_message": "assistant"}.get(event_type)
    message = payload.get("message")
    if role is None or not isinstance(message, str):
        return None, _diagnostic(
            CodexDiagnosticCode.UNKNOWN_EVENT,
            record.envelope,
            f"unrecognized event_msg payload type: {event_type!r}",
        )
    if role == "user" and _is_injected_user_element(message):
        return None, _diagnostic(
            CodexDiagnosticCode.EXCLUDED_INJECTED,
            record.envelope,
            "injected user context is deliberately non-searchable",
        )
    message, repaired = repair_unstorable_text(message)
    if not is_unicode_scalar_text(message):
        return None, _diagnostic(
            CodexDiagnosticCode.INVALID_UNICODE,
            record.envelope,
            "event message contains a non-scalar Unicode value",
        )
    if not message:
        return None, _diagnostic(
            CodexDiagnosticCode.EMPTY_CONTENT,
            record.envelope,
            "recognized event message has empty content",
        )
    return (
        _native_message(
            record=record,
            context=context,
            source_session_id=source_session_id,
            session_kind=session_kind,
            epoch=epoch,
            role=role,
            content_class=ContentClass.PROSE,
            text=message,
            family=CodexRecordFamily.EVENT_MESSAGE,
        ),
        (
            _diagnostic(
                CodexDiagnosticCode.REPAIRED_UNICODE,
                record.envelope,
                "event message contained unstorable code points repaired with U+FFFD",
            )
            if repaired
            else None
        ),
    )


def _parse_compaction(
    record: _DecodedRecord,
    *,
    source_session_id: str,
    session_kind: SessionKind,
    next_epoch: int,
) -> SessionEpochBoundary | None:
    """Recognize the audited message/replacement_history compaction shape."""
    payload = record.payload.get("payload")
    if (
        not _is_json_object(payload)
        or not isinstance(payload.get("message"), str)
        or not isinstance(payload.get("replacement_history"), list)
    ):
        return None
    timestamp = record.payload.get("timestamp")
    alias = _physical_alias(record.envelope, source_session_id)
    return SessionEpochBoundary(
        provider=Provider.CODEX,
        source_session_id=source_session_id,
        session_kind=session_kind,
        conversation_epoch=next_epoch,
        physical_alias=alias,
        timestamp=timestamp if isinstance(timestamp, str) else "",
        trigger="codex_compacted",
        token_count=None,
    )


def _source_diagnostics(
    values: tuple[SourceDiagnostic, ...],
) -> tuple[CodexDiagnostic, ...]:
    """Carry relevant Task 2 source failures into provider coverage."""
    return tuple(
        CodexDiagnostic(
            code=CodexDiagnosticCode.PARTIAL_TAIL,
            detail=value.detail,
            record_ordinal=value.record_ordinal,
            source_line=value.source_line,
            source_byte_offset=value.source_byte_offset,
        )
        for value in values
        if value.code is SourceDiagnosticCode.PARTIAL_TAIL
    )


def _metadata_outer_shape(record: _DecodedRecord) -> bool:
    """Require the exact audited metadata wrapper and a valid timestamp."""
    return set(record.payload) == {"type", "timestamp", "payload"} and (
        isinstance(record.payload.get("timestamp"), str)
        and is_valid_native_timestamp(record.payload["timestamp"])
    )


def _is_string_list(value: object) -> bool:
    """Narrow one audited metadata field to a list of strings."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _turn_context_shape(record: _DecodedRecord) -> bool:
    """Recognize audited turn-context capabilities and field types."""
    payload = record.payload.get("payload")
    if not _metadata_outer_shape(record) or not _is_json_object(payload):
        return False
    fields = set(payload)
    if (
        not fields >= _TURN_CONTEXT_REQUIRED_FIELDS
        or not fields <= _TURN_CONTEXT_FIELDS
    ):
        return False
    if any(
        not isinstance(payload[field], str)
        for field in fields & _TURN_CONTEXT_STRING_FIELDS
    ):
        return False
    if any(
        not _is_json_object(payload[field])
        for field in fields & _TURN_CONTEXT_OBJECT_FIELDS
    ):
        return False
    if any(
        not isinstance(payload[field], bool)
        for field in fields & _TURN_CONTEXT_BOOLEAN_FIELDS
    ):
        return False
    return all(
        _is_string_list(payload[field]) for field in fields & _TURN_CONTEXT_LIST_FIELDS
    )


def _world_state_shape(record: _DecodedRecord) -> bool:
    """Recognize the audited full/state world-state wrapper."""
    payload = record.payload.get("payload")
    return (
        _metadata_outer_shape(record)
        and _is_json_object(payload)
        and set(payload) == {"full", "state"}
        and isinstance(payload.get("full"), bool)
        and _is_json_object(payload.get("state"))
    )


def _inter_agent_metadata_shape(record: _DecodedRecord) -> bool:
    """Recognize the audited trigger-turn inter-agent metadata wrapper."""
    payload = record.payload.get("payload")
    return (
        set(record.payload) >= {"type", "timestamp", "payload"}
        and isinstance(record.payload.get("timestamp"), str)
        and is_valid_native_timestamp(record.payload["timestamp"])
        and _is_json_object(payload)
        and set(payload) >= {"trigger_turn"}
        and isinstance(payload.get("trigger_turn"), bool)
    )


def _token_usage_shape(value: object) -> bool:
    """Require native integer counters; cache writes default to zero in Codex."""
    if not _is_json_object(value) or not set(value) >= _TOKEN_USAGE_REQUIRED_COUNTERS:
        return False
    counters = _TOKEN_USAGE_REQUIRED_COUNTERS | (
        {"cache_write_input_tokens"} if "cache_write_input_tokens" in value else set()
    )
    return all(
        isinstance(value[field], int) and not isinstance(value[field], bool)
        for field in counters
    )


def _token_usage_record_shape(record: _DecodedRecord) -> bool:
    """Recognize token accounting with required minima and additive metadata."""
    payload = record.payload.get("payload")
    return (
        set(record.payload) >= {"type", "timestamp", "payload"}
        and isinstance(record.payload.get("timestamp"), str)
        and is_valid_native_timestamp(record.payload["timestamp"])
        and _is_json_object(payload)
        and all(isinstance(payload.get(field), str) for field in _TOKEN_USAGE_ID_FIELDS)
        and all(_token_usage_shape(payload.get(field)) for field in _TOKEN_USAGE_FIELDS)
    )


def _excluded_outer_metadata_diagnostic(record: _DecodedRecord) -> CodexDiagnostic:
    """Classify audited outer metadata while retaining its prior failure policy."""
    if record.payload.get("type") == "token_usage_record":
        recognized = _token_usage_record_shape(record)
        return _diagnostic(
            CodexDiagnosticCode.EXCLUDED_METADATA
            if recognized
            else CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
            record.envelope,
            "token accounting is deliberately non-searchable"
            if recognized
            else "token_usage_record does not match an audited metadata shape",
        )
    if _inter_agent_metadata_shape(record):
        return _diagnostic(
            CodexDiagnosticCode.EXCLUDED_INTER_AGENT_METADATA,
            record.envelope,
            "inter-agent metadata is retained as provenance, not searchable text",
        )
    return _diagnostic(
        CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
        record.envelope,
        "inter-agent metadata does not match an audited shape",
    )


def _candidate_alias(candidate: PhysicalMessageCandidate) -> PhysicalAlias:
    """Return the sole physical occurrence of an unpaired parser candidate."""
    return candidate.message.identity.physical_aliases[0]


def _messages_touching_new_candidates(
    canonical: CanonicalizationResult,
    candidates: tuple[PhysicalMessageCandidate, ...],
) -> tuple[NativeMessage, ...]:
    """Return only canonical rows created or changed by this parsed suffix."""
    aliases = {_candidate_alias(candidate) for candidate in candidates}
    return tuple(
        message
        for message in canonical.messages
        if aliases.intersection(message.identity.physical_aliases)
    )


def _next_trailing_candidate(
    canonical: CanonicalizationResult,
    candidates: tuple[PhysicalMessageCandidate, ...],
    conversation_epoch: int,
) -> PhysicalMessageCandidate | None:
    """Carry only the last still-unpaired projection in the current epoch."""
    current = tuple(
        candidate
        for candidate in candidates
        if candidate.message.conversation_epoch == conversation_epoch
        and candidate.record_family
        in {
            CodexRecordFamily.RESPONSE_MESSAGE,
            CodexRecordFamily.EVENT_MESSAGE,
        }
    )
    if not current:
        return None
    candidate = max(
        current,
        key=lambda value: (
            _candidate_alias(value).source_file_relative.as_posix(),
            _candidate_alias(value).record_ordinal,
            _candidate_alias(value).source_byte_offset,
        ),
    )
    alias = _candidate_alias(candidate)
    canonical_message = next(
        message
        for message in canonical.messages
        if alias in message.identity.physical_aliases
    )
    if len(canonical_message.identity.physical_aliases) != 1:
        return None
    diagnostic = next(
        (value for value in canonical.diagnostics if alias in value.physical_aliases),
        None,
    )
    if (
        diagnostic is None
        or diagnostic.code is not CanonicalizationDiagnosticCode.NO_COMPATIBLE_PARTNER
    ):
        return None
    return candidate


def parse_codex_session(
    envelopes: tuple[RecordEnvelope, ...],
    *,
    context: CodexSessionContext,
    source_diagnostics: tuple[SourceDiagnostic, ...] = (),
    prior_state: CodexParserState | None = None,
) -> CodexParseResult:
    """Adapt bounded Codex envelopes and canonicalize physical projections."""
    if prior_state is not None and not isinstance(prior_state, CodexParserState):
        raise TypeError("prior_state must be CodexParserState or None")
    state = prior_state if prior_state is not None else CodexParserState()
    records, decode_diagnostics = _decode_records(tuple(envelopes))
    if context.cwd is None:
        cwd = None
        for record in records:
            payload = record.payload.get("payload")
            if _is_json_object(payload):
                candidate = payload.get("cwd")
                if isinstance(candidate, str):
                    cwd = candidate
                    break
        context = CodexSessionContext(
            source_session_id=context.source_session_id,
            repository=context.repository,
            cwd=cwd,
        )
    session_kind, kind_diagnostics = _continued_session_kind(
        records, state.session_kind
    )
    source_session_id, identity_diagnostics = _resolved_session_identity(
        records, context, state.source_session_id
    )
    diagnostics = [
        *decode_diagnostics,
        *identity_diagnostics,
        *kind_diagnostics,
        *_source_diagnostics(tuple(source_diagnostics)),
    ]
    if source_session_id is None:
        return CodexParseResult(
            source_session_id=None,
            session_kind=SessionKind.UNKNOWN,
            messages=(),
            boundaries=(),
            diagnostics=tuple(
                sorted(
                    diagnostics,
                    key=lambda value: (
                        value.record_ordinal
                        if value.record_ordinal is not None
                        else -1,
                        value.code.value,
                        value.detail,
                    ),
                )
            ),
            next_state=state,
        )
    if state.trailing_candidate is not None:
        carry_locator = state.trailing_candidate.message.identity.canonical_locator
        if carry_locator.source_session_id != source_session_id:
            raise ValueError("trailing_candidate belongs to a different source session")
    candidates: list[PhysicalMessageCandidate] = []
    boundaries: list[SessionEpochBoundary] = []
    conversation_epoch = state.next_conversation_epoch
    seen_compaction_digests = set(state.seen_compaction_digests)

    for record in records:
        outer_type = record.payload.get("type")
        if outer_type == "session_meta":
            continue
        if outer_type == "turn_context":
            if not _turn_context_shape(record):
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
                        record.envelope,
                        "turn_context does not match an audited metadata shape",
                    )
                )
        elif outer_type == "world_state":
            if not _world_state_shape(record):
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_METADATA_SHAPE,
                        record.envelope,
                        "world_state does not match an audited metadata shape",
                    )
                )
        elif outer_type == "response_item":
            parsed, item_diagnostics = _parse_response_item(
                record,
                context=context,
                source_session_id=source_session_id,
                session_kind=session_kind,
                epoch=conversation_epoch,
            )
            candidates.extend(parsed)
            diagnostics.extend(item_diagnostics)
        elif outer_type == "event_msg":
            parsed, event_diagnostic = _parse_event(
                record,
                context=context,
                source_session_id=source_session_id,
                session_kind=session_kind,
                epoch=conversation_epoch,
            )
            if parsed is not None:
                candidates.append(parsed)
            if event_diagnostic is not None:
                diagnostics.append(event_diagnostic)
        elif outer_type == "compacted":
            boundary = _parse_compaction(
                record,
                source_session_id=source_session_id,
                session_kind=session_kind,
                next_epoch=conversation_epoch + 1,
            )
            if boundary is None:
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.UNKNOWN_COMPACTION,
                        record.envelope,
                        "compacted record does not match an audited boundary shape",
                    )
                )
            elif record.envelope.source_digest in seen_compaction_digests:
                diagnostics.append(
                    _diagnostic(
                        CodexDiagnosticCode.DUPLICATE_COMPACTION,
                        record.envelope,
                        "duplicate compacted record does not advance the epoch",
                    )
                )
            else:
                seen_compaction_digests.add(record.envelope.source_digest)
                conversation_epoch += 1
                boundaries.append(boundary)
        elif outer_type in {"inter_agent_communication_metadata", "token_usage_record"}:
            diagnostics.append(_excluded_outer_metadata_diagnostic(record))
        else:
            diagnostics.append(
                _diagnostic(
                    CodexDiagnosticCode.UNKNOWN_OUTER_TYPE,
                    record.envelope,
                    f"unrecognized Codex outer record type: {outer_type!r}",
                )
            )

    new_candidates = tuple(candidates)
    established_kind = (
        session_kind
        if state.session_kind in {None, SessionKind.UNKNOWN}
        else state.session_kind
    )
    compatible_carry = (
        state.trailing_candidate if state.session_kind is session_kind else None
    )
    candidates_with_carry = (
        *((compatible_carry,) if compatible_carry is not None else ()),
        *new_candidates,
    )
    canonical = canonicalize_codex_candidates(candidates_with_carry)
    next_candidate = (
        _next_trailing_candidate(canonical, candidates_with_carry, conversation_epoch)
        if session_kind is established_kind
        else None
    )
    return CodexParseResult(
        source_session_id=source_session_id,
        session_kind=session_kind,
        messages=_messages_touching_new_candidates(canonical, new_candidates),
        boundaries=tuple(boundaries),
        diagnostics=tuple(
            sorted(
                diagnostics,
                key=lambda value: (
                    value.record_ordinal if value.record_ordinal is not None else -1,
                    value.code.value,
                    value.detail,
                ),
            )
        ),
        next_state=CodexParserState(
            next_conversation_epoch=conversation_epoch,
            source_session_id=source_session_id,
            session_kind=established_kind,
            seen_compaction_digests=tuple(sorted(seen_compaction_digests)),
            trailing_candidate=next_candidate,
        ),
    )
