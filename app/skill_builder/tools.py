"""Tool-call requests + gateway-result handling (PRD §8).

The agent never performs side-effects; it REQUESTS them and the gateway
executes, returning the result as subsequent input:

  * request_test_run — gateway runs a reduced-phase real scan (test=true, gated
    on the tenant's prospect_real_tests_enabled) against the current draftConfig
    and returns a results summary. If real tests are disabled it returns an
    explanation (not a run) — surfaced conversationally.
  * request_finalize — gateway runs the atomic finalize (create the skills row +
    connect + notify + link evidence). Production scans stay blocked pending the
    existing SU activation step; the agent adds no activation authority.
    NOTE: the created skill's status is `tested` ONLY when the session has a real
    test run behind it, and `configured` otherwise. Do not restate `tested` as the
    outcome here or in any operator-facing message; three repos shipped that claim
    and all three corrected it on 2026-08-04. R6 SHIPPED on the gateway 2026-08-05,
    so both statuses are now reachable — which makes naming either one worse, not
    better, since the outcome depends on session history we cannot see from here.

Two halves:
  1. Emit (agent → gateway): validate the draftConfig (require_complete — you
     only test/finalize a complete config) AND the tool args against the
     tool-call schema BEFORE emitting. Local failures are surfaced in the
     conversation and the tool call is NOT emitted — cheaper than a round-trip
     rejection.
  2. Result (gateway → agent): parse the returned result. A rejection (schema
     violation or org-coupling lint failure — §9/§10) is repaired in the
     conversation, NEVER treated as terminal.

Wire shapes here are provisional: the gateway publishes the real tool-call
arg schemas + result shape (contract #3, PRD §14). Arg validation reads the
(swappable) stub from `app.skill_builder.contracts.tool_schemas()`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.skill_builder import (
    authoring_placeholders,
    contracts,
    org_coupling,
    validator,
)
from app.skill_builder.protocol.agui import AGUIEmitter, InterruptReason
from app.skill_builder.state import PHASES
from app.skill_builder.validator import ValidationIssue

logger = logging.getLogger(__name__)


class ToolName(StrEnum):
    REQUEST_TEST_RUN = "request_test_run"
    REQUEST_FINALIZE = "request_finalize"


class ToolResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    DECLINED = "declined"  # e.g. real tests disabled → explanation, not a run


class RejectionKind(StrEnum):
    SCHEMA_VIOLATION = "schema_violation"
    ORG_COUPLING = "org_coupling"  # a literal where a context-field key is required (R12)
    OTHER = "other"


class ToolRejectionIssue(BaseModel):
    location: str = "/"
    message: str
    kind: RejectionKind = RejectionKind.OTHER

    @field_validator("kind", mode="before")
    @classmethod
    def _tolerate_an_unknown_kind(cls, value: Any) -> Any:
        """An unrecognised `kind` becomes `other` instead of failing the turn.

        We told the gateway that `other` is the default when a kind is absent, and
        a catch-all member implies tolerance — but a *new* kind value was a hard
        `ValidationError`, which `parse_tool_result` turns into a RUN_ERROR that
        kills the whole turn. So the gateway adding a fourth rejection kind would
        have broken every rejection instead of degrading, losing the operator's
        repair path at exactly the moment they needed it.

        Safe precisely because `kind` is presentational: nothing branches on it,
        it is rendered into the repair message. Contrast `status` below, which is
        deliberately strict.
        """
        if value is None:
            return RejectionKind.OTHER
        if isinstance(value, RejectionKind):
            return value
        try:
            return RejectionKind(str(value))
        except ValueError:
            logger.info("unknown rejection kind %r treated as 'other'", value)
            return RejectionKind.OTHER


class ToolResult(BaseModel):
    """Provisional parse of a gateway tool-call result (PRD §8, contract TBD).

    `extra="ignore"` because the gateway owns this shape and may carry more.
    """

    model_config = ConfigDict(extra="ignore")

    tool_name: ToolName
    #: Deliberately STRICT, unlike `ToolRejectionIssue.kind`. Each value drives
    #: different behaviour — rejected routes to repair, declined asks the operator,
    #: succeeded-finalize is terminal — so there is no honest generalisation for an
    #: unknown one: guessing would either repair a turn that finished or end a turn
    #: that needed repair. A loud failure is the correct outcome, and the asymmetry
    #: with `kind` is intentional rather than an oversight.
    status: ToolResultStatus
    summary: dict[str, Any] | str | None = None  # test-run results summary
    explanation: str | None = None  # DECLINED reason (e.g. tests disabled)
    issues: list[ToolRejectionIssue] = Field(default_factory=list)  # REJECTED detail


@dataclass
class ToolCallOutcome:
    """What an emit attempt did. `requested=False` means local validation
    blocked the call and issues were surfaced instead."""

    requested: bool
    tool_call_id: str | None = None
    issues: list[ValidationIssue] = field(default_factory=list)


def _arg_issues(tool: ToolName, args: dict[str, Any]) -> list[ValidationIssue]:
    """Validate tool args against the (swappable) tool-call schema, reusing the
    shared validator mapping (DRY)."""
    schema = contracts.tool_schemas()[tool.value]["input_schema"]
    return validator.issues_for_schema(schema, args)


def _format_issues(issues: list[ValidationIssue]) -> str:
    return "\n".join(f"  - {i.location}: {i.message}" for i in issues)


def _phase_from_issues(issues: list[ValidationIssue]) -> str | None:
    """The authoring section an issue points at, or None if it names none.

    Locations arrive in two spellings and both must work: the schema validator
    emits JSON-pointer-ish (`/discovery/sources/web`) while the R12 and
    placeholder lints emit dotted paths (`discovery.sources.web.queries[0]`).
    Handling only one would silently return None for half the issues — the
    no-phase interrupt this exists to fix.

    Returns the FIRST issue that names a phase rather than trying to summarise
    several: the operator repairs one section at a time, and a wrong single
    phase is worse than the honest None we fall back to (a top-level issue like
    a missing `vertical` genuinely belongs to no section).
    """
    for issue in issues:
        head = issue.location.lstrip("/").split(".")[0].split("/")[0].split("[")[0]
        if head in PHASES:
            return head
    return None


def _finalize_only_issues(draft_config: dict[str, Any]) -> list[ValidationIssue]:
    """Checks that apply to finalizing, but not to a test run.

    A test run is a rehearsal — it neither writes a `skills` row nor competes in
    the R13 catalog, so a missing `vertical` costs nothing there. Finalize does
    both, and that is where it becomes permanent.

    ⚠️ **This is deliberately STRICTER than the ratified schema**, which declares
    `vertical` but leaves it out of the root `required` (root required is
    `version` + `run_parameters`). So the gateway's own finalize gate would
    accept what this refuses, and that asymmetry is intentional rather than an
    oversight:

    a skill finalized with `vertical: null` is invisible to R13 forever. The
    kickoff correctly reports "I didn't find an existing skill", so the next
    session for the same vertical builds another unmatchable one, and the
    library-first premise quietly stops holding — with nothing failing. An
    extra question to the operator is a very cheap alternative.

    Raised with the gateway so `vertical` can join root `required`; if it does,
    this check becomes redundant rather than wrong, and `validate_config` will
    cover it.
    """
    vertical = draft_config.get("vertical")
    if isinstance(vertical, str) and vertical.strip():
        return []
    return [
        ValidationIssue(
            location="vertical",
            message=(
                "is missing, so this skill could never be matched by the "
                "library-first catalog check (R13 dimension 1 of 3) and every "
                "future session would build another one. Ask the operator what "
                "vertical this customer is in, then set it."
            ),
        )
    ]


def _emit_or_block(
    emitter: AGUIEmitter,
    tool: ToolName,
    args: dict[str, Any],
    draft_config: dict[str, Any],
    blocked_intro: str,
) -> ToolCallOutcome:
    # A test/finalize only makes sense on a complete config (PRD §7.3/§7.4).
    issues = validator.validate_config(draft_config, require_complete=True)
    # R12 runs at the gateway's test-run and finalize gates too, and it is NOT
    # schema-expressible — so without this a wrong binding passes every check we
    # make and is rejected after the tool call, where the repair loop has least
    # to work with.
    issues += org_coupling.lint_org_coupling(draft_config)
    # Unfilled brackets and inline `{context_ref:…}` — both pass strict schema
    # AND R12 (they are plain strings, not binding objects), which is how a real
    # session persisted four of them with every gate clean (#27 §3).
    #
    # Placed HERE and deliberately not at the section-proposal gate. Backend
    # runs the same lint at test-run/finalize only, on the grounds that a
    # half-written query mid-conversation is a normal intermediate state and
    # rejecting it would stall the authoring loop. Matching their placement is
    # the point: a port that fires where theirs does not produces a verdict
    # that disagrees with the one gating finalize. We still gain the earlier
    # catch, because this gate precedes their gate.
    issues += authoring_placeholders.lint_authoring_placeholders(draft_config)
    if tool is ToolName.REQUEST_FINALIZE:
        issues += _finalize_only_issues(draft_config)
    issues += _arg_issues(tool, args)
    if issues:
        emitter.message(f"{blocked_intro}\n{_format_issues(issues)}")
        # Name the section to repair. This used to interrupt with no `phase` at
        # all, which reads as "something is wrong, somewhere" — and in the
        # all-accepted state it is a dead end: frontend's `canFinalize` requires
        # `testSatisfied`, which is exactly what a blocked test run leaves false,
        # so the operator gets an interrupt naming nothing and no enabled
        # affordance (#27 turn 10).
        #
        # The REASON stays `awaiting_phase_acceptance` deliberately. Contract #5
        # closed that vocabulary on our word, so inventing a "repair_needed"
        # value would put a string on the wire no consumer can render while
        # every gate still looked green. `phase` is optional by contract, so
        # populating it is strictly additive.
        # Both outcomes are logged because NEITHER was: a tool call is a request
        # for the gateway to spend real money (a test scan) or to make a skill
        # permanent, and until now the runtime recorded neither the request nor
        # the refusal. From outside, "the model never tried" and "we blocked it"
        # looked the same - and the second is a repairable state the operator is
        # sitting in.
        logger.info(
            "tool call BLOCKED: tool=%s issues=%s locations=%s",
            tool.value,
            len(issues),
            [i.location for i in issues],
        )
        emitter.interrupt(
            InterruptReason.AWAITING_PHASE_ACCEPTANCE,
            phase=_phase_from_issues(issues),
        )
        return ToolCallOutcome(requested=False, issues=issues)
    tool_call_id = emitter.tool_call(tool.value, args)
    logger.info("tool call emitted: tool=%s id=%s", tool.value, tool_call_id)
    return ToolCallOutcome(requested=True, tool_call_id=tool_call_id)


def request_test_run(
    emitter: AGUIEmitter, draft_config: dict[str, Any], *, notes: str | None = None
) -> ToolCallOutcome:
    """Emit a request_test_run tool call (or block + surface issues)."""
    args: dict[str, Any] = {"draft_config": draft_config}
    if notes is not None:
        args["notes"] = notes
    return _emit_or_block(
        emitter,
        ToolName.REQUEST_TEST_RUN,
        args,
        draft_config,
        blocked_intro="I can't run a test yet — the draft config isn't ready:",
    )


def request_finalize(
    emitter: AGUIEmitter,
    draft_config: dict[str, Any],
    *,
    slug: str | None = None,
    notes: str | None = None,
) -> ToolCallOutcome:
    """Emit a request_finalize tool call (or block + surface issues)."""
    args: dict[str, Any] = {"draft_config": draft_config}
    if slug is not None:
        args["slug"] = slug
    if notes is not None:
        args["notes"] = notes
    return _emit_or_block(
        emitter,
        ToolName.REQUEST_FINALIZE,
        args,
        draft_config,
        blocked_intro="I can't finalize yet — the draft config isn't ready:",
    )


def parse_tool_result(payload: Any) -> ToolResult:
    """Parse a gateway tool-result (dict or JSON string) into a ToolResult.

    Raises ValueError if it can't be interpreted; the runtime wraps this so an
    unparseable result becomes an in-stream RUN_ERROR rather than a crash.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool result is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"tool result must be an object, got {type(payload).__name__}")
    try:
        return ToolResult.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — normalize to ValueError for the caller
        raise ValueError(f"unrecognized tool result shape: {exc}") from exc


def _summarize_rejection(result: ToolResult) -> str:
    if not result.issues:
        return "  - (no detail provided)"
    return "\n".join(f"  - [{i.kind}] {i.location}: {i.message}" for i in result.issues)


def handle_tool_result(emitter: AGUIEmitter, result: ToolResult) -> None:
    """React to a gateway tool result (PRD §8). A rejection is never terminal —
    it routes back into phase iteration for repair."""
    if result.status == ToolResultStatus.REJECTED:
        emitter.message(
            "The gateway rejected the request:\n"
            f"{_summarize_rejection(result)}\n"
            "Let's fix these and try again — no work is lost."
        )
        emitter.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE)
        return

    if result.status == ToolResultStatus.DECLINED:
        reason = result.explanation or "the gateway declined the request."
        emitter.message(f"The gateway didn't run it: {reason}")
        emitter.interrupt(InterruptReason.AWAITING_DECISION, step="request_declined")
        return

    # SUCCEEDED
    if result.tool_name == ToolName.REQUEST_TEST_RUN:
        summary = (
            result.summary
            if isinstance(result.summary, str)
            else json.dumps(result.summary or {}, indent=2, sort_keys=True)
        )
        emitter.message(
            f"Test run complete. Results summary:\n{summary}\n"
            "Want to accept these results, or revise a section?"
        )
        emitter.interrupt(InterruptReason.AWAITING_DECISION, step="review_test_results")
        return

    # request_finalize succeeded → terminal (skill created + connected). Production
    # scans stay blocked pending the SU activation step (PRD §8) — we say so.
    #
    # Deliberately names NO status. This message used to promise `tested`, which
    # the gateway's finalize sets only when the session has a real test run behind
    # it, and `configured` otherwise. When that was written R6 was blocked, so
    # every finalize landed `configured` and the sentence was false for EVERY real
    # outcome. Frontend shipped the identical claim in two places and backend's
    # tool description in a third, all corrected on 2026-08-04 — three repos
    # asserting one status nobody had checked.
    #
    # R6 shipped 2026-08-05, so both statuses are reachable now. That does not
    # make naming one safe again: which one you get depends on whether this
    # session ran a test, which is gateway-side state this runtime never sees.
    # Reporting only what we actually know (created, connected, activation
    # pending) needed no status then and needs none now — which is why R6 landing
    # required no change here.
    emitter.message(
        "Done — the skill has been created and connected to the org. Its status "
        "reflects whether a passing test run backed it. Production scans stay "
        "blocked until a super-user completes activation."
    )
    emitter.run_finished({"outcome": "finalized"})
