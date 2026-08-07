"""Invoke the deployed runtime once and validate the AG-UI stream it returns.

This answers the question local testing cannot: does the runtime, as AgentCore
actually runs it, emit a well-formed AG-UI event stream that the gateway's R2 pipe
can forward verbatim?

    python scripts/smoke_invoke.py --arn <runtime-arn>
    python scripts/smoke_invoke.py --arn <arn> --continuation   # needs model access
    python scripts/smoke_invoke.py --arn <arn> --raw            # print every frame

**The default invocation is FREE and needs no Bedrock model access.** With no prior
messages the runtime takes its deterministic kickoff path — it names the customer,
runs the R13 catalog match, emits a draft skeleton and interrupts for confirmation,
all without calling a model. That is the whole reason this is useful before the
account's marketplace gate clears: it proves the deployment, the AGUI protocol
binding and the event envelope independently of model access.

`--continuation` sends a real conversation and WILL call the model. Expect it to fail
with a Bedrock 403 until the marketplace subscription exists; that failure arrives as
a well-formed in-stream `RUN_ERROR`, which is itself worth seeing once.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

import boto3
from botocore.config import Config as BotoConfig

REGION = "us-east-1"

#: Events every turn must produce, in order, regardless of path.
REQUIRED_BOOKENDS = ("RUN_STARTED", "RUN_FINISHED")

#: A turn that ends in RUN_ERROR is a legitimate outcome (a model refusal, say), so it
#: is accepted as a terminal event — but reported loudly rather than counted as a pass.
TERMINAL = ("RUN_FINISHED", "RUN_ERROR")


def _payload(continuation: bool) -> dict:
    context = {
        "organization_name": "Franklin HVAC Co",
        "industry": "HVAC",
        "lead_type": "B",
    }
    if not continuation:
        # Kickoff: no prior messages -> deterministic path, no model call.
        return {
            "messages": [],
            "state": {"draftConfig": {}, "acceptance": {}},
            "forwardedProps": {"customer_context": context},
        }
    return {
        "messages": [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "Confirm the customer and I'll begin."},
            {"role": "user", "content": "Yes, that's right. Let's do geography."},
        ],
        "state": {"draftConfig": {}, "acceptance": {}},
        "forwardedProps": {"customer_context": context},
    }


def _events(body: bytes) -> list[dict]:
    """Parse an SSE body into AG-UI event dicts.

    Deliberately tolerant of blank lines and non-`data:` frames, and deliberately
    NOT tolerant of malformed JSON on a data frame — a frame the gateway cannot
    parse is exactly the defect this script exists to catch, so it surfaces rather
    than being skipped.
    """
    events: list[dict] = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw:
            continue
        events.append(json.loads(raw))
    return events


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arn", required=True, help="the runtime ARN")
    parser.add_argument("--qualifier", default="DEFAULT")
    parser.add_argument(
        "--continuation",
        action="store_true",
        help="send a real conversation (CALLS THE MODEL; 403s without marketplace access)",
    )
    parser.add_argument("--raw", action="store_true", help="print every event in full")
    args = parser.parse_args()

    # retries=1 on purpose. botocore's DEFAULT retry policy re-sends on a read timeout,
    # and each retry starts ANOTHER turn server-side while the first is still running —
    # so a slow model call becomes duplicate billed turns, and the traceback blames a
    # timeout while saying nothing about the extras. The sibling ground-truth runtime
    # learned this the expensive way (five paid browser sessions from one call); here
    # the cost is model spend and a confusing transcript rather than browsers.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=BotoConfig(
            read_timeout=240, connect_timeout=15, retries={"total_max_attempts": 1}
        ),
    )

    payload = _payload(args.continuation)
    mode = "continuation (model-backed)" if args.continuation else "kickoff (no model)"
    print(f"invoking {args.arn}")
    print(f"mode: {mode}\n")

    response = client.invoke_agent_runtime(
        agentRuntimeArn=args.arn,
        # Must be 33+ characters — the API rejects a short id rather than padding it,
        # which is why two hex UUIDs are concatenated.
        runtimeSessionId=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
        payload=json.dumps(payload).encode("utf-8"),
        qualifier=args.qualifier,
    )

    body = response["response"].read()
    try:
        events = _events(body)
    except json.JSONDecodeError as exc:
        print(f"MALFORMED event frame — the gateway could not parse this either: {exc}")
        print(body[:4000])
        return 1

    if not events:
        print("No AG-UI events in the response. Raw body:")
        print(body[:4000])
        return 1

    print("=== EVENT SEQUENCE ===")
    for event in events:
        kind = event.get("type", "<no type>")
        if args.raw:
            print(json.dumps(event, indent=2))
        else:
            detail = ""
            if kind == "TEXT_MESSAGE_CONTENT":
                detail = f"  {event.get('delta', '')[:100]}…"
            elif kind == "RUN_FINISHED":
                detail = f"  {json.dumps(event.get('result', {}))}"
            elif kind == "RUN_ERROR":
                detail = f"  {event.get('message', '')[:200]}"
            print(f"  {kind}{detail}")
    print()

    kinds = [e.get("type") for e in events]
    problems = []
    if kinds[0] != "RUN_STARTED":
        problems.append(f"first event is {kinds[0]!r}, expected RUN_STARTED")
    if kinds[-1] not in TERMINAL:
        problems.append(f"last event is {kinds[-1]!r}, expected one of {TERMINAL}")
    for required in REQUIRED_BOOKENDS:
        if required not in kinds and required != "RUN_FINISHED":
            problems.append(f"missing {required}")

    if problems:
        print("=== FAILED ===")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    if kinds[-1] == "RUN_ERROR":
        print("=== RAN, BUT TERMINATED IN RUN_ERROR ===")
        print("The stream is well-formed, so the protocol binding works. Read the")
        print("message above — a Bedrock 403 here means the marketplace gate, not a bug.")
        return 1

    print("=== PASSED ===")
    print(f"{len(events)} events, well-formed, terminated in RUN_FINISHED.")
    print("The gateway can pipe this stream verbatim (R2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
