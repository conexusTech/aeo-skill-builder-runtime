# Admin request — one IAM execution role + one ECR repository

**Account:** `082585646836` · **Region:** `us-east-1` · **Requested by:** Leo Lindo

**One blocking item, plus one optional tidy-up.**

1. 🔴 **An IAM execution role** — `iam:CreateRole` is denied to us. This is the only
   thing that actually blocks the deployment.
2. ⚪ **Optional: an ECR repository `aeo-skill-builder` + push access.** Not blocking —
   we have a working alternative. Our ECR grant is resource-scoped to
   `aeo-groundtruth/*`, where we *can* create and push, so we can ship from
   `aeo-groundtruth/skill-builder`. That name is misleading (it is not ground truth),
   so a correctly-named repo would be nicer, but it is cosmetic. Skip this if it is
   any friction.

Runtime creation itself (`bedrock-agentcore:*`) is already permitted on Leo's
credentials.

**This is modelled on `AmazonBedrockAgentCoreAEOGroundTruthBrowserRole`**, which you
created on 2026-07-31. The trust policy is identical in shape. The permission policy
is the same one with the browser and Secrets Manager statements **removed** and
Bedrock model invocation **added**.

## Why these permissions, and why they are safe to grant as written

**They are copied from a role you already created and one that already works.** The
trust policy is the same shape as
`AmazonBedrockAgentCoreAEOGroundTruthBrowserRole` (yours, 2026-07-31). The Bedrock
statement is modelled on the **working** `AgentCore-aeoskills-produ-ApplicationAgentAeoAvLead-…`
execution role, which invokes models in this account today — deliberately, so this is
not a new permission shape anyone has to reason about from scratch.

Where we differ from that working role, we are **narrower**, not wider:

| | This request | The existing working role |
|---|---|---|
| foundation-model | `…/anthropic.claude-*` | `…/*` (any model) |
| inference-profile | `…/us.anthropic.claude-*` | `…/*` (any model) |

The one place we deliberately match it rather than tightening is the **region wildcard
on the inference-profile ARN**. Cross-region inference profiles (the `us.` prefix) route
across regions, and pinning the ARN to `us-east-1` risks a denial that looks identical to
the grant never landing — while `iam:PutRolePolicy` is denied to us, so correcting it
would cost a second round trip with you. The account scope (`082585646836`) still applies.

**No `aws-marketplace:*` is requested**, deliberately: the existing working role carries
none, which confirms model access on this account is account-level state rather than a
role grant. See the optional ask at the bottom.

## Three things that matter

1. **Please keep the role name exactly `AmazonBedrockAgentCoreAEOSkillBuilderRole`.**
   AWS's own AgentCore policy scopes `iam:PassRole` to
   `arn:aws:iam::*:role/AmazonBedrockAgentCore*`. A different name means a second,
   bespoke policy is also needed.
2. **Please keep the inline policy name exactly `AEOSkillBuilderRuntimePolicy`.**
3. **`us-east-1` only** — our AgentCore grant has an `aws:RequestedRegion` condition
   that denies other regions.

## Run these two commands

```bash
cat > /tmp/skillbuilder-trust.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AgentCoreRuntimeAssumesThisRole",
      "Effect": "Allow",
      "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "082585646836" },
        "ArnLike": { "aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:082585646836:*" }
      }
    }
  ]
}
JSON

cat > /tmp/skillbuilder-runtime.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeTheChatModel",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:CountTokens"
      ],
      "Resource": [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:082585646836:inference-profile/us.anthropic.claude-*"
      ]
    },
    {
      "Sid": "EcrTokenCannotBeScoped",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PullTheRuntimeImage",
      "Effect": "Allow",
      "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      "Resource": "arn:aws:ecr:us-east-1:082585646836:repository/*"
    },
    {
      "Sid": "LogGroups",
      "Effect": "Allow",
      "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/runtimes/*"
    },
    {
      "Sid": "LogResourcePolicy",
      "Effect": "Allow",
      "Action": ["logs:PutResourcePolicy"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/runtimes/aeo_skill_builder-*"
    },
    {
      "Sid": "DescribeLogGroupsIsAccountWide",
      "Effect": "Allow",
      "Action": ["logs:DescribeLogGroups"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:*"
    },
    {
      "Sid": "LogStreams",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
    },
    {
      "Sid": "Observability",
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Metrics",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": { "StringEquals": { "cloudwatch:namespace": "bedrock-agentcore" } }
    },
    {
      "Sid": "OwnWorkloadIdentityOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:us-east-1:082585646836:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:us-east-1:082585646836:workload-identity-directory/default/workload-identity/aeo_skill_builder-*"
      ]
    }
  ]
}
JSON

aws iam create-role \
  --role-name AmazonBedrockAgentCoreAEOSkillBuilderRole \
  --assume-role-policy-document file:///tmp/skillbuilder-trust.json

aws iam put-role-policy \
  --role-name AmazonBedrockAgentCoreAEOSkillBuilderRole \
  --policy-name AEOSkillBuilderRuntimePolicy \
  --policy-document file:///tmp/skillbuilder-runtime.json
```

## Please send back

The role ARN, which will be:

```
arn:aws:iam::082585646836:role/AmazonBedrockAgentCoreAEOSkillBuilderRole
```

## While you are in there — a separate, optional ask

Bedrock model access on this account is not established: every Claude invocation is
refused with *"not authorized to perform the required AWS Marketplace actions
(`aws-marketplace:ViewSubscriptions`, `aws-marketplace:Subscribe`)"*. That gate is
**account-level**, so the role above will deploy fine and reach `READY`, but model
calls will still fail until it clears.

It is not blocking this request — we get a working runtime and a real event stream
without it — but if it is easy for you to clear in the same session, it saves a
later round trip. Two parts:

1. The **Anthropic use-case details form** on the Bedrock "Model access" console
   page. It has never been submitted for this account
   (`aws bedrock get-use-case-for-model-access` → *"You have not filled out the
   request form"*).
2. `aws-marketplace:ViewSubscriptions` + `Subscribe`, then subscribing
   `anthropic.claude-sonnet-5`.
