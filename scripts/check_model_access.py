"""Is Claude Sonnet 5 actually enabled on this account? Read-only, no model call.

Run this instead of asking, and instead of inferring from a 403 — as a principal
without `aws-marketplace:ViewSubscriptions`, an invocation error looks identical
whether a subscription exists or not. `agreementAvailability` is the only reliable
signal we can read.

    python scripts/check_model_access.py

DONE means `anthropic.claude-sonnet-5` shows agreement AVAILABLE. Listing every
Anthropic model is deliberate: if a subset is enabled, that is a different situation
from nothing having been done, and it needs a different follow-up.

Context: request-3-model-subscription.md.
"""

import boto3

bedrock = boto3.client("bedrock", region_name="us-east-1")

models = [
    m["modelId"]
    for m in bedrock.list_foundation_models()["modelSummaries"]
    if m["modelId"].startswith("anthropic.")
]
# De-dup: list_foundation_models repeats ids across inference types.
seen: list[str] = []
for m in models:
    if m not in seen:
        seen.append(m)

print(f"{len(seen)} Anthropic models listed in us-east-1\n")
print(f"{'model':<46} {'agreement':<16} {'entitlement'}")
print("-" * 82)

available = []
for model_id in seen:
    try:
        r = bedrock.get_foundation_model_availability(modelId=model_id)
        agree = r.get("agreementAvailability", {}).get("status", "?")
        ent = r.get("entitlementAvailability", "?")
    except Exception as exc:  # noqa: BLE001
        agree, ent = f"ERR {type(exc).__name__}", "-"
    mark = "  <-- OURS" if model_id == "anthropic.claude-sonnet-5" else ""
    if agree == "AVAILABLE":
        available.append(model_id)
    print(f"{model_id:<46} {agree:<16} {ent}{mark}")

print()
print(f"models WITH an agreement: {len(available)}")
for m in available:
    print("   ", m)
if not available:
    print("    (none — nothing has been subscribed on this account yet)")
