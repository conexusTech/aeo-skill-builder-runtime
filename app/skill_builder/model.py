"""The model seam (PRD §6 model choice Q6, §7.2 phase proposals).

The runtime is model-agnostic behind `ChatModel`: given the composed prompt +
conversation, the model returns a structured `ModelDecision` (propose a section,
await the human, request a test/finalize, …). The deterministic runtime then
does the protocol work — validate, emit STATE_DELTA / tool calls / interrupts.
This keeps the model producing CONFIG content while emission + validation stay
testable without a live model (tests inject `FakeChatModel`).

Production uses `BedrockChatModel` (Q6: Claude Sonnet 5 on Bedrock, Opus 4.8
lever via SKILL_BUILDER_MODEL_ID). The `anthropic` SDK is imported lazily inside
that class so importing this module — and the whole test suite — never requires
the SDK to be installed. `anthropic[bedrock]` is a DEPLOY-TIME dependency; add it
before running the AgentCore runtime.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    # Type-only: keeps the runtime import of the SDK lazy (see BedrockChatModel)
    # while letting mypy check the `messages.create` call against the real
    # TypedDicts. `anthropic` became a real installed dependency with the
    # ground-truth work and ships a py.typed marker, so these are checkable —
    # the alternative, an `ignore_missing_imports` override, silenced genuine
    # errors in exactly the module that uses the SDK most.
    from anthropic.types import MessageParam, TextBlockParam

from app.skill_builder.prompt import PromptComposition
from app.skill_builder.protocol.agui import InterruptReason, Message, TokenUsage

logger = logging.getLogger(__name__)

# Actions the model may choose each turn. Kept as a plain str set (not the
# InterruptReason enum) — this is the model↔runtime contract, decoupled from the
# wire protocol.
ACTIONS = (
    "propose_section",  # propose/revise a phase section → STATE_DELTA
    "request_test_run",
    "request_finalize",
    "connect_existing",  # connect+customize a library match (R13)
    "await_human",  # nothing to emit; wait on the operator
)


class ModelDecision(BaseModel):
    """One turn's structured decision from the model."""

    model_config = ConfigDict(extra="ignore")

    action: str
    message: str
    phase: str | None = None
    section: dict[str, Any] | None = None
    slug: str | None = None
    notes: str | None = None
    interrupt_reason: str | None = None
    #: Usage for the call that produced THIS decision. The runtime accumulates it
    #: onto the emitter; a turn with a repair loop makes several calls (#14).
    #: Not part of the model's structured output -- the client fills it in from the
    #: provider response, so a model cannot influence what a session is billed.
    usage: TokenUsage | None = None


class ChatModel(ABC):
    """Injectable model. Sync to match the deterministic runtime; the AgentCore
    server calls it inside its request handler."""

    @abstractmethod
    def decide(
        self,
        *,
        prompt: PromptComposition,
        messages: list[Message],
        draft_config: dict[str, Any],
        open_phase: str | None,
    ) -> ModelDecision: ...


class FakeChatModel(ChatModel):
    """Test double. Returns a scripted decision (or a deterministic default:
    propose an empty section for the open phase)."""

    def __init__(self, decision: ModelDecision | None = None) -> None:
        self._decision = decision

    def decide(
        self,
        *,
        prompt: PromptComposition,
        messages: list[Message],
        draft_config: dict[str, Any],
        open_phase: str | None,
    ) -> ModelDecision:
        if self._decision is not None:
            return self._decision
        return ModelDecision(
            action="propose_section",
            message=f"Here's a first pass at the {open_phase} section.",
            phase=open_phase,
            section={},
        )


# JSON Schema for the model's structured output. Hand-written to satisfy the
# structured-output constraints (all objects closed; the free-form section is
# returned as a JSON STRING and parsed here, avoiding an open object in the
# schema). Kept in sync with ModelDecision by the tests.
_DECISION_WIRE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "message": {"type": "string"},
        "phase": {"type": "string"},
        "section_json": {
            "type": "string",
            "description": "JSON-encoded config section object (for propose_section).",
        },
        "slug": {"type": "string"},
        "notes": {"type": "string"},
        # Enum-constrained, not a bare string: the emitter coerces an unknown
        # value anyway, but a model that never proposes one avoids the coercion
        # silently changing what it meant to say.
        "interrupt_reason": {
            "type": "string",
            "enum": [r.value for r in InterruptReason],
        },
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}


#: The decision envelope as a TOOL rather than `output_config.format`.
#:
#: Structured outputs are documented as available on Bedrock, but the **Mantle**
#: endpoint rejects them: `output_config.format: Extra inputs are not permitted`
#: (measured, not inferred — `output_config.effort` is accepted on the same
#: endpoint, so it is `format` specifically, and `strict: true` on a tool is
#: rejected too). That left this runtime with no way to get a structured
#: decision out of the model at all: the prompt never described this envelope,
#: so the shape rode entirely on the parameter Mantle refuses.
#:
#: The description is the whole contract the model gets, which is why it states
#: WHEN to call rather than only what it does.
_DECISION_TOOL: dict[str, Any] = {
    "name": "emit_decision",
    "description": (
        "Emit your decision for this turn. Call this exactly once on every "
        "turn — it is the only way your response reaches the operator. Do not "
        "reply in prose instead; prose is not delivered. Put what the operator "
        "should read in `message`, and when you are proposing or revising a "
        "config section, put that section in `section_json` as a JSON-encoded "
        "string.\n\n"
        "`section_json` must be the section's BODY ONLY — do NOT wrap it in "
        "its own name. For the 'geography' section, send "
        '{"home_markets": ..., "targeting": ...}, NOT '
        '{"geography": {"home_markets": ...}}. The name is already carried in '
        "`phase`; wrapping it nests the section under itself and silently "
        "corrupts the config."
    ),
    "input_schema": _DECISION_WIRE_SCHEMA,
}


class BedrockChatModel(ChatModel):
    """Claude on Amazon Bedrock via the Mantle client (Q6).

    Model id from settings (default `anthropic.claude-sonnet-5`; Opus 4.8 lever).
    Adaptive thinking on. Manual prompt-cache breakpoint on the stable prompt
    prefix (baseline + customer context + identity) — Bedrock has no automatic
    caching and the stateless model re-sends history every turn, so this is the
    main cost lever (§13). Structured output via `output_config.format`.

    Live Bedrock behaviour is NOT exercised by the test suite (FakeChatModel is
    used there); verify against Bedrock before production.
    """

    def __init__(
        # 8000 was too small and turn 4 of the first real multi-turn session
        # died on it (#27): `max_tokens` caps thinking AND the answer together,
        # so on a long conversation adaptive thinking can consume the whole
        # budget and the response comes back with a thinking block and nothing
        # else — no tool_use, no text. Doubled rather than maximised: this call
        # is non-streaming and sits in a threadpool, and a very large budget
        # risks an HTTP timeout instead of a truncation.
        self, *, model_id: str, aws_region: str, max_tokens: int = 16000
    ) -> None:
        # Lazy import so this module imports without the anthropic SDK present.
        from anthropic import AnthropicBedrockMantle

        self._client = AnthropicBedrockMantle(aws_region=aws_region)
        self._model_id = model_id
        self._max_tokens = max_tokens

    def decide(
        self,
        *,
        prompt: PromptComposition,
        messages: list[Message],
        draft_config: dict[str, Any],
        open_phase: str | None,
    ) -> ModelDecision:
        stable, volatile = prompt.split()
        system: list[TextBlockParam] = [
            # Stable prefix — the cache breakpoint. Byte-stable across a session.
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
            # Volatile per-turn task + tool schemas after the breakpoint.
            {"type": "text", "text": volatile},
        ]
        wire_messages = _to_anthropic_messages(messages, draft_config, open_phase)
        response = self._client.messages.create(
            model=self._model_id,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=wire_messages,
            tools=[_DECISION_TOOL],
            # DELIBERATELY NOT `tool_choice={"type": "tool", ...}`. Measured
            # against the live Mantle endpoint on a task needing arithmetic:
            # forcing the tool SUPPRESSES the thinking block entirely, so the
            # model reasons inside the argument field instead — and it emitted
            # an `answer` that contradicted its own `reason` (13 widgets vs the
            # 12 it had just derived), in 676 output tokens. Unforced, on the
            # identical prompt: thinking block present, answer correct and
            # self-consistent, 331 tokens. Forcing costs accuracy AND doubles
            # the bill; it is not the safe-looking option it appears to be.
        )
        # Filtering is required, not defensive: adaptive thinking means
        # `content` also carries ThinkingBlocks. Discriminate on `b.type`
        # directly rather than `getattr(b, "type", None)` — the getattr defeats
        # type narrowing AND is quietly worse at runtime, since an SDK rename
        # would yield no blocks and surface as a JSON decode error on "" instead
        # of an attribute error naming the real cause.
        # Logged on EVERY call, not just failures: #27 had to root-cause a
        # truncation from a stack trace because neither stop_reason nor the
        # budget appeared anywhere in our logs. One line per turn is cheap
        # next to a cross-repo investigation.
        logger.info(
            "decide: stop_reason=%s blocks=%s output_tokens=%s max_tokens=%s",
            response.stop_reason,
            [b.type for b in response.content],
            getattr(response.usage, "output_tokens", None),
            self._max_tokens,
        )
        if response.stop_reason == "max_tokens":
            logger.warning(
                "model hit max_tokens (%s) — thinking and answer share this "
                "budget, so the decision may be truncated or absent",
                self._max_tokens,
            )

        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        if tool_use is not None:
            decision = _decision_from_mapping(dict(tool_use.input))
        else:
            # The model answered in prose instead of calling the tool. Rare,
            # but `tool_choice` is unforced by design (see above), so it is
            # reachable. The text is only parseable if it happens to be the
            # JSON envelope; when it is not, json.loads raises and the turn
            # becomes an in-stream RUN_ERROR, which is the honest outcome —
            # inventing a decision here would put words in the operator's
            # conversation that the model never chose.
            text = next((b.text for b in response.content if b.type == "text"), "")
            if not text.strip():
                # Neither a tool call nor any text. The usual cause is
                # `stop_reason == "max_tokens"` with adaptive thinking eating
                # the whole budget, leaving a thinking block and nothing else
                # — which is exactly how #27's turn 4 died. Previously this
                # fell through to json.loads("") and surfaced as an opaque
                # JSONDecodeError that named the parser instead of the cause,
                # and cost the reporting team a CloudWatch dig to root-cause.
                raise RuntimeError(
                    "model returned no decision: stop_reason="
                    f"{response.stop_reason!r}, blocks="
                    f"{[b.type for b in response.content]}, "
                    f"max_tokens={self._max_tokens}, "
                    f"output_tokens={getattr(response.usage, 'output_tokens', None)}"
                    + (
                        " — the budget was consumed before an answer was"
                        " produced; raise max_tokens or lower effort."
                        if response.stop_reason == "max_tokens"
                        else ""
                    )
                )
            decision = _parse_wire_decision(text)
        decision.usage = _usage_from_response(response)
        return decision


def _usage_from_response(response: Any) -> TokenUsage:
    """Map the SDK's usage onto the #14 wire names.

    The names differ and the mapping is not guessable, which is why it was read
    off `anthropic.types.Usage` rather than assumed: the SDK calls them
    `cache_read_input_tokens` and `cache_creation_input_tokens`, and both are
    OPTIONAL — None when the request had no cache breakpoint. A wrong or unguarded
    mapping yields zeros, and a zero here is indistinguishable from a cache that
    never hit, which is the exact number the `cache_control` lever is tuned by.

    Never raises: usage is billing metadata, and losing a turn because a provider
    changed a usage field would trade a cost figure for a failed conversation. A
    missing count is reported as zero and the turn proceeds.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _to_anthropic_messages(
    messages: list[Message], draft_config: dict[str, Any], open_phase: str | None
) -> list[MessageParam]:
    """Map replayed history to anthropic messages + a final grounding turn.

    Only user/assistant text is carried (tool/system entries are folded into the
    system prompt / handled by the runtime). A trailing user turn states the
    current draft + open phase so the model proposes against real state.
    """
    out: list[MessageParam] = []
    for msg in messages:
        if not isinstance(msg.content, str):
            continue
        # Branch per role rather than passing `msg.role` through: it is a plain
        # `str` on our wire model and MessageParam wants a Literal, so this is
        # the narrowing an `in (...)` test cannot give — and it avoids a `cast`,
        # which would suppress the check instead of satisfying it.
        if msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            out.append({"role": "assistant", "content": msg.content})
    grounding = (
        "Current draft config:\n"
        + json.dumps(draft_config, sort_keys=True)
        + (f"\nOpen phase to work on: {open_phase}." if open_phase else "")
    )
    out.append({"role": "user", "content": grounding})
    return out


def _parse_wire_decision(text: str) -> ModelDecision:
    """Parse a JSON *string* decision into a ModelDecision.

    The prose fallback path only — the tool path hands us an already-parsed
    dict and calls `_decision_from_mapping` directly.
    """
    return _decision_from_mapping(json.loads(text))


def _decision_from_mapping(data: dict[str, Any]) -> ModelDecision:
    """Shared core: wire mapping → ModelDecision (section_json → dict)."""
    section = None
    section_json = data.get("section_json")
    if isinstance(section_json, str) and section_json.strip():
        section = json.loads(section_json)
    return ModelDecision(
        action=data["action"],
        message=data["message"],
        phase=data.get("phase"),
        section=section,
        slug=data.get("slug"),
        notes=data.get("notes"),
        interrupt_reason=data.get("interrupt_reason"),
    )


def get_chat_model(*, model_id: str, aws_region: str) -> ChatModel:
    """Build the production model. Called by the AgentCore server entry."""
    return BedrockChatModel(model_id=model_id, aws_region=aws_region)
