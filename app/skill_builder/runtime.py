"""Turn handler — reconstructs one stateless turn and drives the emitter.

This is the logic behind the AgentCore POST /invocations entry (the actual
`--protocol AGUI` server binding is added at the model milestone). Each turn is
independent: it is rebuilt entirely from the gateway-supplied RunAgentInput
(MESSAGES_SNAPSHOT + STATE_SNAPSHOT), with no reliance on any long-lived
session (PRD §3).

Scope at milestones 1–3 (no model calls yet — PRD §15 stub-testable):
  * Kickoff turn: reflect the customer (name / ICP / lead type) so a wrong-org
    error is caught immediately (PRD §5), seed a schema-valid draftConfig
    skeleton and emit it as a STATE_SNAPSHOT (first proposal → snapshot per §4),
    then RUN_FINISHED with an interrupt awaiting the operator's go-ahead.
  * Continuation turn: acknowledge input and report the next open phase, then
    interrupt awaiting that phase's acceptance. The actual per-phase section
    PROPOSAL content (the STATE_DELTA body) is produced once the model is wired
    (later milestone); the plumbing — deltas, validation, interrupts — is here.
  * Any failure becomes an in-stream RUN_ERROR; the invocation never crashes
    and never surfaces a raw 500 (PRD §4).

The five-layer system prompt is composed here and returned on the TurnResult so
the model milestone can feed it to Bedrock unchanged; nothing calls a model yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.skill_builder import catalog, contracts, draft, org_coupling, tools, validator
from app.skill_builder.context import CustomerContext
from app.skill_builder.model import ChatModel, ModelDecision
from app.skill_builder.prompt import compose
from app.skill_builder.protocol.agui import AGUIEmitter, InterruptReason, RunAgentInput
from app.skill_builder.state import BuilderState

logger = logging.getLogger(__name__)

#: What a client sees when a turn dies unexpectedly. Deliberately opaque — see
#: the handler in `handle_turn` for why the exception text must not go on the wire.
_OPAQUE_FAILURE = "The builder hit an internal error and could not continue this turn."


@dataclass
class TurnResult:
    """What one turn produced: the event stream (emitter) and the composed
    system prompt the model milestone will send to Bedrock."""

    emitter: AGUIEmitter
    system_prompt: str


def handle_turn(
    payload: dict[str, Any],
    *,
    thread_id: str | None = None,
    model: ChatModel | None = None,
) -> TurnResult:
    """Handle one POST /invocations turn. Total-failure-safe (PRD §4).

    `model` drives per-phase section proposals (PRD §7.2). When None, the phase
    path falls back to a deterministic "here's the next section" acknowledgement
    — useful for stub-testing the plumbing without a model (PRD §15)."""
    # Parse defensively — a malformed envelope is an in-stream error, not a 500.
    try:
        run_input = RunAgentInput.model_validate(payload)
    except ValidationError as exc:
        emitter = AGUIEmitter(thread_id=thread_id)
        # Logged because this path is otherwise INVISIBLE: it emits an in-stream
        # RUN_ERROR and still returns HTTP 200, so in CloudWatch it looked
        # identical to a healthy kickoff (a bare `POST /invocations 200`). The
        # detail stays here rather than on the wire for the usual reason - a
        # ValidationError echoes back the payload we were sent.
        logger.warning(
            "turn rejected: malformed RunAgentInput (%s error(s), thread_id=%s)",
            exc.error_count(),
            thread_id,
        )
        emitter.run_error(f"malformed RunAgentInput: {exc.error_count()} error(s)",
                          code="invalid_input")
        return TurnResult(emitter, "")

    emitter = AGUIEmitter(
        thread_id=run_input.thread_id or thread_id, run_id=run_input.run_id
    )
    try:
        ctx = CustomerContext(run_input.forwarded_props.customer_context)
        emitter.run_started()
        pending = run_input.pending_tool_result()
        if pending is not None:
            system_prompt = _after_tool(emitter, ctx, pending)
        elif run_input.is_kickoff:
            system_prompt = _kickoff(emitter, run_input, ctx)
        else:
            system_prompt = _continue(emitter, run_input, ctx, model)
        return TurnResult(emitter, system_prompt)
    except Exception:  # noqa: BLE001 — turn must never crash the invocation
        # Never interpolate the exception into a client-facing event. RUN_ERROR
        # travels agent → gateway → the operator's browser, and provider errors
        # embed our own infrastructure identifiers: a Bedrock 403 names
        # `arn:aws:iam::<account>:user/...` and the account id verbatim. The
        # detail belongs in the runtime's logs, which is where an operator with
        # authority can read it. Same reason the malformed-envelope branch above
        # emits `error_count()` rather than the ValidationError itself.
        logger.exception(
            "skill-builder turn failed (thread_id=%s)",
            run_input.thread_id or thread_id,
        )
        emitter.run_error(_OPAQUE_FAILURE, code="internal_error")
        return TurnResult(emitter, "")


def _kickoff(
    emitter: AGUIEmitter, run_input: RunAgentInput, ctx: CustomerContext
) -> str:
    """First turn: ground on the customer, then library-first (PRD §7.1).

    On a catalog hit, propose connect+customize (no new build yet — R13). On no
    match, seed a build-new skeleton and snapshot it. Either way the opener
    reflects the customer so a wrong-org error is caught immediately (PRD §5).
    """
    facts = ctx.first_message_facts()
    hit = catalog.best_match(
        run_input.forwarded_props.catalog,
        vertical=ctx.vertical,
        lead_type=ctx.lead_type,
    )

    if hit is not None:
        emitter.message(
            f"You're building for {facts['customer']} (lead type: {facts['lead_type']}; "
            f"ICP: {facts['icp']}). There's already an active skill for this "
            f"vertical and lead type — '{hit.name}' ({hit.slug}). I recommend we "
            "connect and customize it rather than build from scratch; it will run "
            "for this org using its own scan-time context. Want to connect it, or "
            "build a new skill instead?"
        )
        emitter.interrupt(
            InterruptReason.AWAITING_DECISION, step="connect_or_build", match_slug=hit.slug
        )
        return _system_prompt(ctx, task=(
            f"Kickoff, library hit: recommend connecting+customizing '{hit.slug}'. "
            "Build new only on explicit operator decline."
        ))

    # No match → build new (PRD §7.1). Seed a schema-valid skeleton and emit it
    # as the first proposal (first proposal → STATE_SNAPSHOT, delta base is
    # ambiguous — PRD §4).
    # When the org's runtime context carries no `industry`, the catalog match
    # could not run at all — `catalog.match` returns [] for a null vertical. The
    # old opener reported "I didn't find an existing skill for this vertical"
    # regardless, which reads as a completed search that came back empty. It is
    # the sentence that makes the problem self-perpetuating: the vertical then
    # finalizes as null, R13 can never match the skill, and the next session
    # says the same thing and builds another one (#27 §3).
    if ctx.vertical:
        match_clause = (
            "I didn't find an existing skill for this vertical and lead type, "
            "so we'll build a new one"
        )
    else:
        match_clause = (
            "I don't have an industry on file for this customer, so I couldn't "
            "check whether a skill already exists — tell me the vertical (for "
            "example 'HVAC' or 'auto parts') and I'll check before we build "
            "anything new"
        )
    emitter.message(
        f"You're building a prospect-scanning skill for {facts['customer']} "
        f"(lead type: {facts['lead_type']}; ICP: {facts['icp']}). {match_clause} "
        "— working through geography, discovery, validation, contacts, and "
        "scoring one section at a time. Confirm the customer is correct and I'll begin."
    )
    skeleton = draft.skeleton(
        name=(ctx.organization_name or "New") + " Prospect Scanner",
        vertical=ctx.vertical,
        # The gateway's runtime-context returns `lead_type` straight from the
        # `organizations.lead_type` enum column, so this is already A / B /
        # MIXED (or null when the org hasn't answered). `skeleton()` drops any
        # non-enum value rather than emitting it — CustomerContext falls back to
        # free-text `onboarding_data.lead_type`, which must not reach the config.
        lead_type=ctx.lead_type,
        product_description=(
            f"Prospect-scanning skill for the {ctx.vertical or 'target'} vertical."
        ),
    )
    run_input.state.draft_config = skeleton
    issues = validator.validate_config(skeleton, require_complete=False)
    if issues:
        # Should not happen for our own skeleton; surface rather than emit bad state.
        emitter.run_error(
            "seed skeleton failed validation: "
            + "; ".join(f"{i.location}: {i.message}" for i in issues),
            code="invalid_config",
        )
        return ""
    emitter.state_snapshot(run_input.state)
    emitter.interrupt(InterruptReason.AWAITING_DECISION, step="kickoff_confirmation")
    return _system_prompt(ctx, task=(
        "Kickoff, no library match: confirm the customer and begin a new build."
    ))


def _continue(
    emitter: AGUIEmitter,
    run_input: RunAgentInput,
    ctx: CustomerContext,
    model: ChatModel | None,
) -> str:
    """Continuation turn: propose the next open phase (model-driven), or — once
    all phases are accepted — offer a test run."""
    state = run_input.state
    # All-accepted is NOT a separate early return any more. It used to emit a
    # canned "ask me to run a test" line and interrupt WITHOUT calling the model,
    # so every later turn produced byte-identical output forever: the operator
    # answering "yes, run a test" was never seen by anything that could act on
    # it, and `TOOL_CALL_*` was structurally unreachable through conversation
    # rather than merely unexercised (#27). Backend proved it from `usage` being
    # absent on both turns — by contract that means no model ran.
    #
    # The message was also a promise the branch could not keep: it invited a
    # request the code had no path to honour. And `request_test_run` /
    # `request_finalize` exist as model ACTIONS, so short-circuiting here made
    # that whole surface — and #13.7's divergence check with it — dead code.
    all_accepted = state.all_phases_accepted()
    phase = None if all_accepted else state.next_open_phase()
    composition = compose(
        customer_context=ctx,
        task=_ALL_ACCEPTED_TASK if all_accepted else _phase_task(state, phase),
        tools=contracts.tool_schemas(),
        context_field_keys=contracts.context_field_keys(),
        config_positions=contracts.config_positions(),
        runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema(),
    )

    if model is None:
        # Deterministic fallback (no model wired). Preserved for both states so
        # the runtime still serves a coherent stream with no model at all —
        # which is what every sibling repo developed against before access
        # landed. Note this path CANNOT emit TOOL_CALL_*, by design: a tool call
        # is a real gateway side effect and must never come from a stub.
        if all_accepted:
            emitter.message(
                "All sections are accepted. When you're ready, ask me to run a "
                "test against the draft config."
            )
            emitter.interrupt(InterruptReason.AWAITING_TEST_RUN)
        else:
            emitter.message(
                f"Got it. The next section to settle is '{phase}'. "
                "I'll propose it, and you can accept or request changes."
            )
            emitter.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE, phase=phase)
        return composition.render()

    decision = model.decide(
        prompt=composition,
        messages=run_input.messages,
        draft_config=state.draft_config,
        open_phase=phase,
    )
    # Record BEFORE applying the decision: `_apply_decision` is what emits the
    # terminal event, and the emitter attaches usage at that point. Recording
    # afterwards would bill every turn at zero — and it would look right, because
    # the RUN_FINISHED still carries a correct outcome (#14).
    if decision.usage is not None:
        emitter.record_usage(decision.usage)
    _apply_decision(emitter, state, decision, open_phase=phase)
    return composition.render()


#: Task for the all-sections-accepted state.
#:
#: Deliberately explicit that acting is allowed, because the previous
#: deterministic branch could only ever repeat an invitation. It also has to say
#: what NOT to do: nothing else stops a model from calling `request_test_run` on
#: the turn that merely accepted the final section, and a test run is a real
#: gateway side effect with real cost — it must follow from the operator asking,
#: not from the state being reached.
_ALL_ACCEPTED_TASK = (
    "Every section is accepted, so authoring is done and there is no next "
    "section to propose.\n"
    "- If the operator has just asked you to run a test, choose "
    "`request_test_run`.\n"
    "- If they have asked you to finalize (and a test run has already "
    "succeeded), choose `request_finalize`.\n"
    "- If they have asked to change something, re-open that section with "
    "`propose_section` for the phase they named.\n"
    "- Otherwise choose `await_human` with interrupt_reason "
    "`awaiting_test_run`, and tell them the draft is ready whenever they are.\n"
    "Do NOT request a test run just because every section is accepted — a test "
    "run costs real money and must follow from what the operator actually asked."
)


def _phase_task(state: BuilderState, phase: str | None) -> str:
    """The per-turn instruction for the open phase — REVISE vs PROPOSE, explicitly.

    This used to be one string, `"Propose or revise the '<phase>' section."`, which
    left the choice to the model on every turn. That is fine for a section with no
    body yet and actively harmful for one being re-opened, because `set_section`
    REPLACES `config[phase]` wholesale: a model that reads "propose" and writes a
    fresh section discards everything the operator already settled there.

    The case is real rather than theoretical (aeo-frontend, thread #24). Once
    per-section change-requests are enabled, an accepted section can have its flag
    cleared and come back through `next_open_phase()`. An operator asking for one
    wording tweak would get the whole section rebuilt — worse than the bug that
    change flow exists to fix, and invisible: the turn succeeds, the config is
    valid, and only someone who remembers the old body would notice.

    A non-empty body is the signal, not the acceptance flag. `skeleton()` seeds
    every phase as `{}`, so emptiness distinguishes "never authored" from
    "authored and re-opened" without needing to know why it re-opened.
    """
    if phase is None:
        return "All sections are accepted; do not propose another."
    if state.draft_config.get(phase):
        return (
            f"REVISE the existing '{phase}' section. It already has content the "
            "operator settled. Change only what their latest message asks for and "
            "carry everything else through unchanged — your section replaces the "
            "previous one wholesale, so anything you omit is deleted."
        )
    return f"Propose the '{phase}' section."


def _apply_vertical(
    emitter: AGUIEmitter, state: BuilderState, decision: ModelDecision
) -> None:
    """Record a vertical the model obtained from the operator, and re-derive the slug.

    Applies on ANY action rather than a dedicated one: the vertical normally
    arrives on an `await_human` turn (the model asked, the operator answered),
    but it could equally ride along with the first `propose_section`. Gating it
    on one action would silently drop the answer on the other.

    Only fills a genuine gap — never overwrites a vertical that came from the
    org's runtime context, which is the authoritative source. And the slug is
    re-derived here because `build_slug` runs at kickoff, when the vertical was
    still unknown, so it had already degenerated to the bare
    `prospect-scanner` (#27 §3).
    """
    supplied = (decision.vertical or "").strip()
    if not supplied:
        return
    existing = state.draft_config.get("vertical")
    if isinstance(existing, str) and existing.strip():
        return

    patch = [{"op": "add", "path": "/vertical", "value": supplied}]
    slug = draft.build_slug(supplied)
    if state.draft_config.get("slug") != slug:
        patch.append(
            {
                "op": "replace" if "slug" in state.draft_config else "add",
                "path": "/slug",
                "value": slug,
            }
        )
    state.draft_config = draft.apply(state.draft_config, patch)
    emitter.state_delta(patch)


def _apply_decision(
    emitter: AGUIEmitter,
    state: BuilderState,
    decision: ModelDecision,
    *,
    open_phase: str | None,
) -> None:
    """Turn a ModelDecision into protocol events (PRD §7.2). All validation and
    emission live here; the model only decides."""
    _apply_vertical(emitter, state, decision)

    if decision.action == "propose_section":
        phase = decision.phase or open_phase or ""
        section = decision.section or {}
        new_config, patch = draft.set_section(state.draft_config, phase, section)
        issues = validator.validate_config(new_config, require_complete=False)
        # R12 at the section gate, not only at the tool gate: this is the earliest
        # point the violation exists and the only one where the model is still
        # holding the section that caused it. Linted over the WHOLE config rather
        # than just this section, deliberately — that is what the gateway does, so
        # narrowing it would make our verdict disagree with the one that gates
        # finalize. A stale violation in an accepted section therefore surfaces
        # here, which is noisier but strictly better than surfacing at finalize.
        issues += org_coupling.lint_org_coupling(new_config)
        if issues:
            # Don't emit an invalid delta — surface + re-open the phase to fix.
            emitter.message(
                f"That '{phase}' proposal doesn't validate:\n"
                + "\n".join(f"  - {i.location}: {i.message}" for i in issues)
            )
            emitter.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE, phase=phase)
            return
        state.draft_config = new_config
        emitter.message(decision.message)
        emitter.state_delta(patch)
        emitter.interrupt(InterruptReason.AWAITING_PHASE_ACCEPTANCE, phase=phase)
        return

    if decision.action == "request_test_run":
        emitter.message(decision.message)
        outcome = tools.request_test_run(emitter, state.draft_config, notes=decision.notes)
        if outcome.requested:
            emitter.run_finished({"outcome": "tool_call", "tool": "request_test_run"})
        return

    if decision.action == "request_finalize":
        emitter.message(decision.message)
        outcome = tools.request_finalize(
            emitter, state.draft_config, slug=decision.slug, notes=decision.notes
        )
        if outcome.requested:
            emitter.run_finished({"outcome": "tool_call", "tool": "request_finalize"})
        return

    if decision.action == "connect_existing":
        emitter.message(decision.message)
        emitter.interrupt(
            InterruptReason.AWAITING_DECISION, step="connect", match_slug=decision.slug
        )
        return

    # await_human / anything unrecognized → surface + wait on the operator.
    emitter.message(decision.message)
    emitter.interrupt(decision.interrupt_reason or InterruptReason.AWAITING_DECISION)


def _after_tool(emitter: AGUIEmitter, ctx: CustomerContext, pending: Any) -> str:
    """React to a gateway tool result (PRD §8). A rejection routes back into
    phase iteration; a decline/success is surfaced conversationally. Never
    terminal except a successful finalize."""
    result = tools.parse_tool_result(pending)
    # This path calls no model, so it emits no `decide:` line and used to be
    # indistinguishable from a kickoff in the logs - which meant the first
    # `request_test_run` and `request_finalize` ever executed on this feature
    # (2026-08-12) left NO trace in the runtime's own logs. A tool result is the
    # single most interesting event a turn can carry; it should not be the
    # quietest.
    logger.info(
        "tool result received: tool=%s status=%s", result.tool_name, result.status
    )
    tools.handle_tool_result(emitter, result)
    return _system_prompt(
        ctx, task=f"Discuss the {result.tool_name} result (status: {result.status})."
    )


def _system_prompt(ctx: CustomerContext, *, task: str) -> str:
    """Compose the five-layer prompt for the (future) model call and render it."""
    composition = compose(
        customer_context=ctx,
        task=task,
        tools=contracts.tool_schemas(),
        context_field_keys=contracts.context_field_keys(),
        config_positions=contracts.config_positions(),
        runtime_populated=contracts.runtime_populated_positions(),
        config_schema=contracts.config_schema(),
    )
    return composition.render()
