# Request #3 — subscribe the account to Claude Sonnet 5

**Account:** `082585646836` · **Region:** `us-east-1` · **Requested by:** Leo Lindo

> ✅ **Requests #1 and #2 are both DONE and both worked.** The execution role exists,
> the `bedrock-mantle` permissions are applied, and the runtime is deployed and `READY`.
> Nothing to redo. **This is one console action, and it is the last one.**

## The one thing needed

**Subscribe this AWS account to `anthropic.claude-sonnet-5`** in the Bedrock →
"Model access" / AWS Marketplace console.

That is the whole request. No IAM change, no code change, nothing to send back.

## How to confirm it worked

```bash
aws bedrock get-foundation-model-availability \
  --region us-east-1 --model-id anthropic.claude-sonnet-5
```

**Done means `agreementAvailability` reads `AVAILABLE`.** Right now it reads
`NOT_AVAILABLE`, and it is the only field that does:

```json
{
  "modelId": "anthropic.claude-sonnet-5",
  "agreementAvailability": { "status": "NOT_AVAILABLE" },   ← the only gap
  "authorizationStatus": "AUTHORIZED",
  "entitlementAvailability": "AVAILABLE",
  "regionAvailability": "AVAILABLE"
}
```

Three of the four are already green: the region offers the model, the account is
entitled to it, and authorization is in place. Only the AWS Marketplace agreement is
missing. Please check that field rather than relying on the console appearing to have
accepted something.

## Why this is now certain rather than suspected

Earlier we were unsure whether a subscription was really missing, because our own
invocations failed with permission errors that look identical whether a subscription
exists or not. That ambiguity is gone: with `bedrock-mantle` granted, the **runtime's
own role** now gets far enough to try, and the failure is explicit:

```
Your subscription to the model could not be established. Reason:
User: arn:aws:sts::082585646836:assumed-role/AmazonBedrockAgentCoreAEOSkillBuilderRole/…
is not authorized to perform: aws-marketplace:Subscribe on resource: *
```

The Anthropic SDK's Mantle client tries to establish the subscription **at call time**.
Once the account is subscribed there is nothing left for it to establish, and the call
proceeds.

## Please do NOT solve this by granting the role `aws-marketplace:Subscribe`

It would work, and it is the wrong fix. That action **cannot be scoped to one model** —
the denial above shows it evaluated on `resource: *` — so granting it would let this
runtime subscribe the account to *any* AWS Marketplace product, and incur the charges.
A one-time subscription by a human removes the need for any principal to hold it.

If it turns out the role additionally needs read-only
`aws-marketplace:ViewSubscriptions` after the subscription exists, we will come back for
that single narrow action rather than pre-emptively asking for it now.

## On the Anthropic use-case form — probably not needed

`aws bedrock get-use-case-for-model-access` reports *"You have not filled out the
request form"*, but `authorizationStatus` above already reads `AUTHORIZED`. Those two
signals disagree and we cannot resolve it from outside the console. **Treat the
subscription as the goal**; fill in the form only if the console blocks on it.

## What happens once this lands

Nothing on your side. We redeploy nothing — the runtime is already running the right
image with the right role — we simply re-invoke and confirm. The full chat loop then
works end to end for the first time, which unblocks nine of the feature's thirteen
acceptance criteria across three repos.
