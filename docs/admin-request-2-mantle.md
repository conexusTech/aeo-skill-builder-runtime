# Admin request #2 — one IAM statement + enable Claude Sonnet 5

**Account:** `082585646836` · **Region:** `us-east-1` · **Requested by:** Leo Lindo

**Two parts, both needed, both in this document.** Part 1 is an IAM statement on an
existing role. Part 2 is enabling the model on the account. Neither works without the
other, so please do them together.

> ✅ **Request #1 is DONE — nothing to redo.** You created
> `AmazonBedrockAgentCoreAEOSkillBuilderRole` earlier today, exactly as asked, and it
> worked: the runtime deployed and reached `READY` first attempt. That request is closed
> and lives in `admin-request-skillbuilder-role.md` purely as a record.
>
> **This is a separate ask.** Part 1 adds one statement to that same role; Part 2
> enables the model on the account. Nothing here re-does request #1.

## Part 1 — the IAM statement

Add permission for the **`bedrock-mantle`** service to the existing inline policy
`AEOSkillBuilderRuntimePolicy`:

```json
{
  "Sid": "InvokeViaMantleWhichIsADifferentService",
  "Effect": "Allow",
  "Action": "bedrock-mantle:*",
  "Resource": "arn:aws:bedrock-mantle:us-east-1:082585646836:project/*"
}
```

## Why it was missed

Not a change of plan — we asked for the wrong service, and the mistake was invisible
until a real model call ran.

**`bedrock-mantle` is a third, distinct service.** The AWS Bedrock family has at least
three separately-authorized services, and permission on one grants nothing on another:

| Service | What uses it | In request #1? |
|---|---|---|
| `bedrock-agentcore` | hosting the runtime | ✅ already on Leo's user |
| `bedrock` | the "classic" `InvokeModel` API | ✅ granted — **but we never call it** |
| **`bedrock-mantle`** | **the client we actually ship** | ❌ **missing — this request** |

Our runtime uses the Anthropic SDK's *Mantle* client, which calls
`bedrock-mantle:CreateInference`. The `bedrock:InvokeModel` you granted is never reached
on that path. The policy was internally consistent and simply named the wrong service,
which is why review would not have caught it. The live error:

```
User: arn:aws:sts::082585646836:assumed-role/AmazonBedrockAgentCoreAEOSkillBuilderRole/…
is not authorized to perform: bedrock-mantle:CreateInference
on resource: arn:aws:bedrock-mantle:us-east-1:082585646836:project/default
```

## Why the action wildcard, when everything else we asked for was scoped

`bedrock-mantle` has **no public service model in botocore**, so its action list cannot
be enumerated — we know `CreateInference` only because the error named it, and a
streaming turn or a token count may call siblings we cannot predict. A grant on a
*nearly* right action denies byte-for-byte identically to no grant at all, so a narrower
guess most likely costs you another round trip rather than saving anything.

The resource stays fully scoped: this account, this region, project resources only. If
you would still prefer the narrow form, `bedrock-mantle:CreateInference` alone will get
the next turn further, and we will come back if it names another action.

## ⚠️ `put-role-policy` REPLACES — it does not merge

Applying only the statement above would **delete the other ten**. Please apply the
complete document below, which is the granted policy plus the one new statement.

```bash
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
      "Sid": "InvokeViaMantleWhichIsADifferentService",
      "Effect": "Allow",
      "Action": "bedrock-mantle:*",
      "Resource": "arn:aws:bedrock-mantle:us-east-1:082585646836:project/*"
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

aws iam put-role-policy \
  --role-name AmazonBedrockAgentCoreAEOSkillBuilderRole \
  --policy-name AEOSkillBuilderRuntimePolicy \
  --policy-document file:///tmp/skillbuilder-runtime.json
```

**Do not create a new role and do not rename the policy** — both names above must stay
exactly as they are, for the same reasons as request #1.

## 🔴 Part 2 — the IAM statement alone will NOT be enough. Please do this too.

**If you have already been asked to enable Claude Sonnet 5, this section says exactly
what is still outstanding and how to confirm it — please read it rather than skipping
as done.** As of 2026-08-07 it is not yet in place.

**The authoritative check, which anyone can run:**

```bash
aws bedrock get-foundation-model-availability \
  --region us-east-1 --model-id anthropic.claude-sonnet-5
```

Today it returns:

```json
{
  "modelId": "anthropic.claude-sonnet-5",
  "agreementAvailability": { "status": "NOT_AVAILABLE" },   ← THE ONE THAT MATTERS
  "authorizationStatus": "AUTHORIZED",
  "entitlementAvailability": "AVAILABLE",
  "regionAvailability": "AVAILABLE"
}
```

**Three of the four are already green.** The region offers the model, the account is
entitled to it, and authorization is in place. **The only gap is
`agreementAvailability: NOT_AVAILABLE` — the AWS Marketplace agreement, i.e. the
subscription itself.**

✅ **Done means that field reads `AVAILABLE`.** That is the single check to run
afterwards; please do not rely on the console appearing to have accepted something.

**On the Anthropic use-case form — needed only if the console asks.** We report it
because `aws bedrock get-use-case-for-model-access` says *"You have not filled out the
request form"*, but `authorizationStatus` is already `AUTHORIZED`, so the two signals
disagree and we cannot tell from outside whether the form is a hard prerequisite here.
Treat the subscription as the goal and the form as a step only if the console blocks on
it.

**Why our own invocations cannot answer this**, in case they get quoted at you:
`user/leo.lindo` holds neither `aws-marketplace:Subscribe` nor `ViewSubscriptions`, so
every attempt returns a permissions error that looks the same whether a subscription
exists or not. The availability API above is the only reliable signal we have.

**The Mantle client attempts to subscribe on demand, at call time.** That is the part
that matters for the role: once `bedrock-mantle:*` is granted, the runtime's role will
reach exactly this same point and fail the same way — unless a subscription already
exists.

**What we are asking for: subscribe the account to `anthropic.claude-sonnet-5` once,
in the Bedrock / Marketplace console.** Nothing else in Part 2.

**And explicitly NOT `aws-marketplace:Subscribe` on the role.** That action cannot be
scoped to a single model — the denial evaluates it on `resource: *` — so granting it
would let our runtime subscribe the account to *any* marketplace product and bill for
it. A one-time subscription by you removes the need for any principal to hold it.

**Why the role still needs Part 1 even after you subscribe:** the Anthropic SDK's Mantle
client establishes its subscription at call time, so it is the caller that gets denied.
Once the agreement exists account-wide there is nothing left to establish, and the
`bedrock-mantle:*` statement is what lets the runtime actually issue the inference. If
it turns out to also need read-only `aws-marketplace:ViewSubscriptions`, we will come
back for that one narrow action rather than pre-emptively asking for it.

**Order:** subscribe first, then Part 1 — but both in one sitting is fine, since neither
is usable without the other.

## Nothing to send back

We can read the role (`iam:GetRolePolicy` is granted), so we will verify it ourselves
and redeploy. A one-line "done" is enough.
