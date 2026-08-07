# Admin request #2 — add one statement to an existing role

**Account:** `082585646836` · **Region:** `us-east-1` · **Requested by:** Leo Lindo

> ✅ **Request #1 is DONE — nothing to redo.** You created
> `AmazonBedrockAgentCoreAEOSkillBuilderRole` earlier today, exactly as asked, and it
> worked: the runtime deployed and reached `READY` first attempt. That request is closed
> and lives in `admin-request-skillbuilder-role.md` purely as a record.
>
> **This is a separate, smaller ask: one additional statement on that same role.**

## What is needed

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

## Nothing to send back

We can read the role (`iam:GetRolePolicy` is granted), so we will verify it ourselves
and redeploy. A one-line "done" is enough.

## Possibly related, if it is easy while you are there

Bedrock model access on this account may also need the **Anthropic use-case details
form** on the Bedrock "Model access" console page, plus a marketplace subscription for
`anthropic.claude-sonnet-5`. **We are no longer sure this is required** — we have never
gotten far enough to hit it, because every attempt so far failed earlier, first in our
own dependencies and now in IAM. Mentioning it only so it is on your radar; please do
not treat it as a blocker on our word.
