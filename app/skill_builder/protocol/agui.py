"""The single owned AG-UI protocol module (PRD §4).

Standardize on the AWS-documented AgentCore AGUI contract ONLY — no CopilotKit,
no `@ag-ui/client`, no `ag-ui-protocol` OSS runtime dependency. Every AG-UI
shape the runtime consumes or emits is defined here and nowhere else, so that
when the pre-1.0 protocol shifts (Risk §17) or the gateway pins the exact wire
shapes (contract #2), there is one file to change.

Wire casing. AG-UI envelope keys are camelCase (`threadId`, `runId`,
`forwardedProps`, `draftConfig`, `messageId`). Models below use camelCase
aliases and are populate-by-name so tests can build them with Python-native
snake_case while serialization emits camelCase (`by_alias=True`). The
`customer_context` blob inside `forwardedProps` is the org runtime-context JSON
(the same payload the scan runtime gets from
GET /runtime/organizations/:orgId/context) and stays snake_case as delivered.

Provisional until contract #2. The event names and field sets encode the spec's
enumerated events (TEXT_MESSAGE_START/CONTENT/END, STATE_DELTA, STATE_SNAPSHOT,
RUN_STARTED/FINISHED, RUN_ERROR, TOOL_CALL_START/ARGS/END). The exact TOOL_CALL_*
field spelling is what we owe the gateway to confirm; keep changes to this file.

Inbound events we consume (in RunAgentInput):
  * ``messages``       — full conversation history, replayed each turn.
  * ``state``          — {draftConfig, acceptance} (see app.skill_builder.state).
  * ``forwardedProps`` — {customer_context} (PRD §5).

Outbound events we emit:
  * TEXT_MESSAGE_START / _CONTENT / _END — streamed assistant text.
  * STATE_DELTA    — RFC 6902 JSON Patch applied to draftConfig (every revision).
  * STATE_SNAPSHOT — full state when a delta base is ambiguous (first proposal
                     or a large restructure).
  * RUN_STARTED / RUN_FINISHED — turn lifecycle; RUN_FINISHED carries an
                     interrupt outcome when the agent is blocked awaiting the
                     human (phase acceptance, a decision) (PRD §4).
  * RUN_ERROR      — in-stream for ANY failure. Never crash the invocation,
                     never surface a raw 500 (PRD §4).
  * TOOL_CALL_START / _ARGS / _END — request a gateway-executed side-effect
                     (request_test_run / request_finalize — see
                     app.skill_builder.tools in a later milestone).

Consistency invariant (R2): a turn interrupted mid-stream must leave state
consistent — the emitter only appends COMPLETE events, and the gateway persists
per full event (last full event wins).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from enum import StrEnum
from itertools import count
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.skill_builder.state import BuilderState

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    """AG-UI event `type` discriminators. AWS-documented names only."""

    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    STATE_DELTA = "STATE_DELTA"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"


# --- Inbound envelope (RunAgentInput) --------------------------------------


class Message(BaseModel):
    """One entry in the replayed conversation history.

    `role` is left as a free string rather than an enum: AG-UI carries
    `user` / `assistant` / `system` / `tool` / `developer` and the gateway may
    introduce more; we must not reject a turn over an unfamiliar role. `content`
    is text for the message roles; tool messages may carry structured content,
    so it is typed permissively.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    role: str
    content: Any = None
    name: str | None = None
    tool_call_id: str | None = Field(default=None, alias="toolCallId")


class ForwardedProps(BaseModel):
    """`forwardedProps` — carries the org runtime-context blob (PRD §5) and the
    active-skills catalog the gateway supplies for the library-first match
    (PRD §11).

    The inner `customer_context` is consumed as-is by app.skill_builder.context;
    `catalog` is a raw list handed to app.skill_builder.catalog, which coerces
    it. Neither is fully modelled here — their shapes are owned elsewhere
    (runtime-context endpoint / gateway catalog), and this keeps the protocol
    module free of domain models. `catalog` delivery here is provisional (PRD §14).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    customer_context: dict[str, Any] = Field(default_factory=dict, alias="customer_context")
    catalog: list[dict[str, Any]] = Field(default_factory=list)


class RunAgentInput(BaseModel):
    """The full inbound payload for one turn (POST /invocations body).

    `extra="ignore"`: the gateway owns this envelope and may grow it; a turn
    must not fail because a field we don't read appeared. `thread_id` is the
    stable builder-session id (PRD §3), also delivered via the
    X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header — we accept it here for
    completeness and cross-check against the header at the runtime boundary.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    thread_id: str | None = Field(default=None, alias="threadId")
    run_id: str | None = Field(default=None, alias="runId")
    messages: list[Message] = Field(default_factory=list)
    state: BuilderState = Field(default_factory=BuilderState)
    forwarded_props: ForwardedProps = Field(
        default_factory=ForwardedProps, alias="forwardedProps"
    )

    @property
    def is_kickoff(self) -> bool:
        """True on the very first turn — no prior assistant message exists yet,
        so the agent opens with the library-first catalog check + its
        understanding of the customer (PRD §7.1)."""
        return not any(m.role == "assistant" for m in self.messages)

    def last_user_text(self) -> str | None:
        """Text of the most recent user message, or None on kickoff."""
        for msg in reversed(self.messages):
            if msg.role == "user":
                return msg.content if isinstance(msg.content, str) else None
        return None

    def pending_tool_result(self) -> Any | None:
        """Content of the LAST message iff it is a tool result the gateway just
        appended for the agent to react to (PRD §8). None otherwise — a stale
        tool message earlier in the history is not pending."""
        if self.messages and self.messages[-1].role == "tool":
            return self.messages[-1].content
        return None


# --- Outbound events -------------------------------------------------------


class _Event(BaseModel):
    """Base for every outbound event. Serializes camelCase, drops unset."""

    model_config = ConfigDict(populate_by_name=True)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_none=True)


class TextMessageStartEvent(_Event):
    type: EventType = EventType.TEXT_MESSAGE_START
    message_id: str = Field(alias="messageId")
    role: str = "assistant"


class TextMessageContentEvent(_Event):
    type: EventType = EventType.TEXT_MESSAGE_CONTENT
    message_id: str = Field(alias="messageId")
    delta: str


class TextMessageEndEvent(_Event):
    type: EventType = EventType.TEXT_MESSAGE_END
    message_id: str = Field(alias="messageId")


#: STATE_DELTA patches address the AG-UI **state envelope**, not the config
#: document. The envelope is `{draftConfig, acceptance}` (see `state.BuilderState`
#: and what STATE_SNAPSHOT emits), so a config revision is rooted here.
#:
#: This is not cosmetic bookkeeping. Callers compute patches against the CONFIG
#: (`draft.diff` / `draft.set_section` operate on `draftConfig`), so an un-rooted
#: op reads `/geography/scope` — which cannot be applied to the very envelope we
#: snapshot: `add /geography/scope` fails with "member 'geography' not found"
#: because its parent doesn't exist at envelope level. Snapshot and delta would
#: disagree about their root with nothing on the wire declaring it. Latent until
#: someone applies one (R2's pipe, frontend's reducer), which is why it survived
#: this long.
#:
#: Pinned with aeo-backend + aeo-frontend 2026-08-03: `/draftConfig/<section>/…`
#: for config, `/acceptance/<section>` for acceptance — acceptance deliberately a
#: SIBLING, never nested inside the config, so conversation state cannot be
#: persisted into `skills.config`.
STATE_CONFIG_ROOT = "/draftConfig"

#: RFC 6902 members whose value is a JSON Pointer and must therefore be re-rooted.
#: `from` matters as much as `path`: `move` / `copy` ops carry it, and re-rooting
#: only `path` would leave them pointing outside the envelope.
_POINTER_MEMBERS = ("path", "from")


def reroot_config_patch(
    patch: list[dict[str, Any]], root: str = STATE_CONFIG_ROOT
) -> list[dict[str, Any]]:
    """Re-root a config-relative RFC 6902 patch onto the state envelope."""
    return [
        {
            key: (f"{root}{value}" if key in _POINTER_MEMBERS else value)
            for key, value in op.items()
        }
        for op in patch
    ]


class StateDeltaEvent(_Event):
    """RFC 6902 JSON Patch against the state envelope. `delta` is the op list.

    Ops are envelope-rooted (`/draftConfig/…`); use `AGUIEmitter.state_delta`,
    which re-roots a config-relative patch for you.
    """

    type: EventType = EventType.STATE_DELTA
    delta: list[dict[str, Any]]


class StateSnapshotEvent(_Event):
    """Full builder state when a delta base is ambiguous (PRD §4)."""

    type: EventType = EventType.STATE_SNAPSHOT
    snapshot: dict[str, Any]


class RunStartedEvent(_Event):
    type: EventType = EventType.RUN_STARTED
    thread_id: str | None = Field(default=None, alias="threadId")
    run_id: str | None = Field(default=None, alias="runId")


class RunFinishedEvent(_Event):
    """End of a turn. `result` carries an interrupt outcome when the agent is
    blocked awaiting the human (PRD §4) — e.g. {"outcome": "interrupt",
    "reason": "awaiting_phase_acceptance", "phase": "geography"}."""

    type: EventType = EventType.RUN_FINISHED
    thread_id: str | None = Field(default=None, alias="threadId")
    run_id: str | None = Field(default=None, alias="runId")
    result: dict[str, Any] | None = None


class RunErrorEvent(_Event):
    """In-stream failure. Emitted instead of crashing the invocation (PRD §4)."""

    type: EventType = EventType.RUN_ERROR
    message: str
    code: str | None = None


class ToolCallStartEvent(_Event):
    type: EventType = EventType.TOOL_CALL_START
    tool_call_id: str = Field(alias="toolCallId")
    tool_call_name: str = Field(alias="toolCallName")
    parent_message_id: str | None = Field(default=None, alias="parentMessageId")


class ToolCallArgsEvent(_Event):
    type: EventType = EventType.TOOL_CALL_ARGS
    tool_call_id: str = Field(alias="toolCallId")
    delta: str


class ToolCallEndEvent(_Event):
    type: EventType = EventType.TOOL_CALL_END
    tool_call_id: str = Field(alias="toolCallId")


# --- Interrupt outcome reasons (RUN_FINISHED.result.reason) ----------------


class InterruptReason(StrEnum):
    """Why the turn is ending blocked-on-human. Read by the gateway to render
    the operator's next action (Accept / Request-changes / trigger test /
    finalize).

    This is the CLOSED wire vocabulary for `result.reason` (contract #5). It can
    only stay closed because `coerce_interrupt_reason` guards the one path where
    a value does not originate here — the model may propose an
    `interrupt_reason` string, and an invented one would put a value on the wire
    that no consumer can render while every gate still looked green.

    `AWAITING_FINALIZE` is declared but currently emitted by no path; see
    `EMITTED_INTERRUPT_REASONS`.
    """

    AWAITING_PHASE_ACCEPTANCE = "awaiting_phase_acceptance"
    AWAITING_DECISION = "awaiting_decision"
    AWAITING_TEST_RUN = "awaiting_test_run"
    AWAITING_FINALIZE = "awaiting_finalize"


#: The subset of `InterruptReason` any current code path actually emits. Kept
#: separate from the enum because "declared" and "reachable" are different facts,
#: and a consumer building a required UI branch for a reason that never arrives is
#: waiting on an event that cannot happen. Asserted by tests so this does not rot.
EMITTED_INTERRUPT_REASONS: frozenset[InterruptReason] = frozenset(
    {
        InterruptReason.AWAITING_PHASE_ACCEPTANCE,
        InterruptReason.AWAITING_DECISION,
        InterruptReason.AWAITING_TEST_RUN,
    }
)


def coerce_interrupt_reason(reason: InterruptReason | str | None) -> InterruptReason:
    """Map an arbitrary reason onto the closed vocabulary, defaulting safely.

    The model can propose `interrupt_reason` as free text. Passed through, an
    invented value ("awaiting_user_input", or prose) would reach the gateway and
    the operator's browser as a `reason` no consumer has a branch for — and
    because the turn otherwise succeeds, nothing would look wrong: the UI would
    simply render no control and the conversation would appear to stall on its
    own. Unknown values become `AWAITING_DECISION`, which is the honest
    generalisation (the turn IS blocked on a human) and always has a control.
    """
    if isinstance(reason, InterruptReason):
        return reason
    if reason is None:
        return InterruptReason.AWAITING_DECISION
    try:
        return InterruptReason(str(reason))
    except ValueError:
        logger.warning(
            "unknown interrupt reason %r coerced to %s",
            reason,
            InterruptReason.AWAITING_DECISION.value,
        )
        return InterruptReason.AWAITING_DECISION


class TokenUsage(BaseModel):
    """Per-turn Bedrock token usage, the carrier for Q3 billing (thread #14).

    The gateway CANNOT observe these: the model call happens inside the AgentCore
    runtime, so this field is the only path by which a builder session's cost ever
    becomes known. Without it every session bills 0.0000 and looks correct doing
    it, next to a turn count that increments perfectly.

    The four counts stay SEPARATE and must never be summed into one input figure:
    cached input bills far below fresh input, and the `cache_control` breakpoint
    on the stable prompt prefix is this feature's main cost lever — a blended
    total both overstates a long session and erases the lever from the only data
    anyone could use to tune it.

    Accumulated with `+` because one turn can make several model calls (a repair
    loop), so this is "what this turn cost", never one call's usage.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def is_empty(self) -> bool:
        """True when nothing was recorded — a turn with no model call at all.

        Distinguishes "no model ran" from "a model ran and cost nothing". The
        second is impossible, so emitting all-zeros would assert something false;
        the emitter omits `usage` entirely in the first case.
        """
        return not any(
            (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )


# --- Emitter ---------------------------------------------------------------


class AGUIEmitter:
    """Builds the outbound event stream for one turn.

    Accumulates COMPLETE events in `self.events` (the R2 consistency invariant:
    the gateway persists per full event, so we never emit a partial one). In
    production a streaming sink drains `encode_sse` over these; in tests the
    list is asserted directly, which is what makes the runtime testable against
    a mocked stream (PRD §15).

    `id_factory` injects message/tool-call id generation so tests are
    deterministic; production passes a uuid factory. It defaults to a simple
    monotonic counter, never `random`/`uuid` implicitly, so a forgotten
    injection can't make a test flaky.
    """

    def __init__(
        self,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self.events: list[_Event] = []
        self._counter = count(1)
        self._id_factory = id_factory or self._default_id
        #: Accumulated across every model call this turn. Held on the emitter, not
        #: threaded through call sites, so a terminal path cannot forget to attach
        #: it — there are four of them and forgetting one bills that turn at zero.
        self.usage = TokenUsage()

    def _default_id(self) -> str:
        return f"msg-{next(self._counter)}"

    def _append(self, event: _Event) -> _Event:
        self.events.append(event)
        return event

    # -- run lifecycle --
    # NOTE: events are constructed with their camelCase wire aliases (threadId,
    # messageId, …). Pydantic accepts aliases by default and this keeps the
    # call sites type-checkable without the pydantic mypy plugin; the Python
    # attribute names stay snake_case per repo style.
    def run_started(self) -> None:
        self._append(RunStartedEvent(threadId=self.thread_id, runId=self.run_id))

    def record_usage(self, usage: TokenUsage) -> None:
        """Add one model call's usage to this turn's total (thread #14)."""
        self.usage = self.usage + usage

    def run_finished(self, result: dict[str, Any] | None = None) -> None:
        # Attach usage here rather than at each terminal call site: there are four
        # of them (interrupt, two tool_call outcomes, finalized) and a missed one
        # would bill that turn at zero while everything else looked right.
        #
        # `result is not None` guard, NOT just a usage check: contract #5 declares
        # `outcome` REQUIRED on any result, so synthesising `{"usage": …}` onto a
        # bare RUN_FINISHED would emit a result the gateway rejects — trading a
        # cost figure for a contract violation. Losing usage on the bare path is
        # the lesser evil and costs nothing today, because every runtime terminal
        # passes an outcome; if a future path needs both, it must supply an
        # outcome rather than have this invent one.
        if result is not None and not self.usage.is_empty():
            result = {**result, "usage": self.usage.model_dump()}
        self._append(
            RunFinishedEvent(threadId=self.thread_id, runId=self.run_id, result=result)
        )

    def interrupt(self, reason: InterruptReason | str, **detail: Any) -> None:
        """RUN_FINISHED with an interrupt outcome (PRD §4).

        `reason` is coerced onto the closed vocabulary — a model-proposed value
        reaches here, and an invented one would render as no control at all while
        the turn reported success. See `coerce_interrupt_reason`.
        """
        result: dict[str, Any] = {
            "outcome": "interrupt",
            "reason": coerce_interrupt_reason(reason).value,
        }
        result.update({k: v for k, v in detail.items() if v is not None})
        self.run_finished(result)

    def run_error(self, message: str, code: str | None = None) -> None:
        self._append(RunErrorEvent(message=message, code=code))

    # -- assistant text --
    def message(self, text: str, *, role: str = "assistant") -> str:
        """Emit a complete assistant message as START → CONTENT → END and
        return its message id. One CONTENT block per call keeps every emitted
        event complete; token-level streaming (many CONTENT deltas) is layered
        on at the model-wiring milestone without changing this contract."""
        message_id = self._id_factory()
        self._append(TextMessageStartEvent(messageId=message_id, role=role))
        self._append(TextMessageContentEvent(messageId=message_id, delta=text))
        self._append(TextMessageEndEvent(messageId=message_id))
        return message_id

    # -- state --
    def state_delta(self, patch: list[dict[str, Any]]) -> None:
        """Emit a config revision as a STATE_DELTA.

        `patch` is CONFIG-relative (as produced by `draft.diff` /
        `draft.set_section`); it is re-rooted onto the state envelope here, which
        is the one place that owns the wire shape. See `STATE_CONFIG_ROOT` for why
        an un-rooted patch cannot be applied to our own STATE_SNAPSHOT.
        """
        self._append(StateDeltaEvent(delta=reroot_config_patch(patch)))

    def state_snapshot(self, state: BuilderState) -> None:
        """Emit the full state (first proposal / large restructure)."""
        self._append(StateSnapshotEvent(snapshot=state.model_dump(by_alias=True)))

    # -- tool calls --
    def tool_call(
        self, name: str, args: dict[str, Any], *, parent_message_id: str | None = None
    ) -> str:
        """Emit a full START → ARGS → END tool-call request and return its id.
        The gateway executes the tool and returns its result as subsequent
        input (PRD §8)."""
        tool_call_id = self._id_factory()
        self._append(
            ToolCallStartEvent(
                toolCallId=tool_call_id,
                toolCallName=name,
                parentMessageId=parent_message_id,
            )
        )
        self._append(ToolCallArgsEvent(toolCallId=tool_call_id, delta=json.dumps(args)))
        self._append(ToolCallEndEvent(toolCallId=tool_call_id))
        return tool_call_id

    # -- serialization --
    def wire_events(self) -> list[dict[str, Any]]:
        """Emitted events as camelCase wire dicts (assertion-friendly)."""
        return [e.to_wire() for e in self.events]

    def sse_stream(self) -> list[str]:
        """Emitted events as encoded SSE frames, in order."""
        return [encode_sse(e) for e in self.events]


def encode_sse(event: _Event) -> str:
    """Encode one event as a Server-Sent-Events frame.

    AG-UI over AgentCore is delivered as SSE (PRD §2). One JSON object per
    `data:` line, terminated by a blank line. Compact separators keep frames
    small (turns are billed per invocation — PRD §13).
    """
    payload = json.dumps(event.to_wire(), separators=(",", ":"))
    return f"data: {payload}\n\n"
