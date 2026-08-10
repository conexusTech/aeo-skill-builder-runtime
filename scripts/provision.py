#!/usr/bin/env python
"""Provision the Conversational Skill Builder AgentCore runtime.

Idempotent: safe to re-run. Creating and updating both keep the SAME runtime ARN,
which matters because that ARN is what the gateway holds for R2 — a redeploy must
not invalidate it.

    python scripts/provision.py --check                 # read-only inventory
    python scripts/provision.py --role-arn <arn>        # normal deploy
    python scripts/provision.py --role-arn <arn> --skip-push

Adapted from the sibling `aeo-groundtruth-browser-runtime/scripts/provision.py`.
Its runbook — `docs/RUNBOOK-agentcore-runtime.md` in that repo — carries the eight
failure modes this account has actually produced. The three that shaped this file
are reproduced inline where they bite, so nobody has to cross-reference to stay safe.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"  # The bedrock-agentcore grant has an aws:RequestedRegion condition.
ACCOUNT = "082585646836"

#: ⚠️ The namespace says "groundtruth" and this is NOT the ground-truth runtime.
#:
#: That is deliberate and it is a permissions artifact, not a mistake. Our ECR grant is
#: resource-scoped: we can create and push under `aeo-groundtruth/*`, and are denied
#: `ecr:CreateRepository` everywhere else — including the `aeo-skill-*` repos that
#: already exist. Verified 2026-08-07 by direct probe on both.
#:
#: 🔴 CHANGING THIS NOW ALSO NEEDS AN IAM CHANGE, which was not true when it was written.
#: The administrator scoped the execution role's `PullTheRuntimeImage` statement to this
#: exact repository ARN (it was `repository/*` as we requested — they tightened it, which
#: is the better call). So flipping this constant to the correctly-named
#: `aeo-skill-builder` would deploy a runtime that cannot pull its own image, and the
#: failure would surface at container start rather than at `update-agent-runtime`.
#: Move the repo and the policy in the same change, or not at all.
ECR_REPO = "aeo-groundtruth/skill-builder"
REPO_URI = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}"

#: Prefixed `AmazonBedrockAgentCore` DELIBERATELY, and it is not cosmetic.
#:
#: AWS's own documented AgentCore policy scopes `iam:PassRole` to
#: `arn:aws:iam::*:role/AmazonBedrockAgentCore*` and its role-management statement to
#: `*BedrockAgentCore*`. A role named anything else needs a bespoke policy written and
#: reviewed by an administrator. Renaming this makes the access request harder to
#: grant, not tidier.
ROLE_NAME = "AmazonBedrockAgentCoreAEOSkillBuilderRole"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
RUNTIME_NAME = "aeo_skill_builder"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def image_tag() -> str:
    """Content-identifying tag: the git commit, plus `-dirty` for uncommitted work.

    Not `latest`. We deploy by DIGEST (see `build_and_push`), and a mutable tag would
    mean the image a session runs is whatever was pushed last.

    Returns `"unknown"` rather than raising when there is no HEAD to read. That is
    only true at repo genesis (a `git init` with no commit yet), but the sibling
    script's version raised `CalledProcessError` there and took `--check` down with
    it — and a read-only inventory that crashes is worse than useless, because it
    reports nothing about the resources it had already inspected.
    """
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    )
    if head.returncode != 0:
        return "unknown"
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.strip()
    sha = head.stdout.strip()
    return f"{sha}-dirty" if dirty else sha


def ensure_ecr(check: bool) -> str | None:
    ecr = boto3.client("ecr", region_name=REGION)
    try:
        ecr.describe_repositories(repositoryNames=[ECR_REPO])
        print(f"[ecr] exists: {REPO_URI}")
        return REPO_URI
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    if check:
        print(f"[ecr] MISSING: {ECR_REPO}")
        return None
    try:
        ecr.create_repository(
            repositoryName=ECR_REPO,
            imageScanningConfiguration={"scanOnPush": True},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("AccessDeniedException", "AccessDenied"):
            raise
        # Name the blocker and keep going, so one run reports everything that is
        # missing rather than only the first thing.
        print(f"[ecr] BLOCKED: this identity cannot create {ECR_REPO}.")
        print("      Our ECR grant is scoped to aeo-groundtruth/* — see the comment on")
        print("      ECR_REPO above, and docs/admin-request-skillbuilder-role.md.")
        return None
    print(f"[ecr] created: {REPO_URI}")
    return REPO_URI


def build_and_push(repo_uri: str) -> str:
    """Build for linux/arm64 and push. Returns a DIGEST reference, not a tag."""
    token = boto3.client("ecr", region_name=REGION).get_authorization_token()
    auth = base64.b64decode(token["authorizationData"][0]["authorizationToken"]).decode()
    _, password = auth.split(":", 1)
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    _run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password.encode(),
    )
    version = image_tag()
    print(f"[image] tag={version}")
    tag = f"{repo_uri}:{version}"
    # Two flags that are both load-bearing:
    #   --platform linux/arm64 : AgentCore requires arm64, and an amd64 image fails at
    #     DEPLOY rather than at build, so the platform is pinned rather than inherited.
    #   --provenance=false     : buildx's default OCI attestation makes the pushed
    #     artifact a manifest LIST, which AgentCore rejects WITHOUT ever mentioning
    #     attestations. This cost the sibling repo a debugging session.
    _run(
        [
            "docker", "buildx", "build",
            "--platform", "linux/arm64",
            "--provenance=false",
            "-t", tag,
            "--push", ".",
        ]
    )
    images = boto3.client("ecr", region_name=REGION).describe_images(
        repositoryName=ECR_REPO, imageIds=[{"imageTag": version}]
    )
    digest = images["imageDetails"][0]["imageDigest"]
    print(f"[image] pushed {ECR_REPO}@{digest}")
    return f"{repo_uri}@{digest}"


def ensure_role(check: bool, supplied_arn: str | None = None) -> str | None:
    """Resolve the execution role. We cannot create it; an administrator must.

    `--role-arn` is the normal path: this script then needs no IAM permission of its
    own. `iam:PassRole` on that role is still required by CreateAgentRuntime, but that
    is the caller's grant, not this script's.
    """
    if supplied_arn:
        print(f"[iam] using supplied role: {supplied_arn}")
        return supplied_arn
    iam = boto3.client("iam")
    try:
        iam.get_role(RoleName=ROLE_NAME)
        print(f"[iam] exists: {ROLE_ARN}")
        return ROLE_ARN
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("NoSuchEntity", "AccessDenied", "AccessDeniedException"):
            raise
        if code == "NoSuchEntity":
            print(f"[iam] MISSING: {ROLE_NAME}")
        else:
            print(f"[iam] cannot read {ROLE_NAME} (iam:GetRole denied)")
    if check:
        return None
    print("[iam] BLOCKED: iam:CreateRole is denied to this identity.")
    print("      Hand docs/admin-request-skillbuilder-role.md to an administrator,")
    print("      then re-run with --role-arn <arn>.")
    return None


def ensure_runtime(container_uri: str, role_arn: str, check: bool) -> str | None:
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    existing = None
    for runtime in control.list_agent_runtimes().get("agentRuntimes", []):
        if runtime["agentRuntimeName"] == RUNTIME_NAME:
            existing = runtime
            break

    if check:
        print(f"[runtime] {'exists' if existing else 'MISSING'}: {RUNTIME_NAME}")
        return existing["agentRuntimeArn"] if existing else None

    artifact = {"containerConfiguration": {"containerUri": container_uri}}

    #: AGUI, not HTTP. This is the whole point of the runtime: the gateway pipes our
    #: AG-UI SSE stream straight through to the browser. `serverProtocol` is an enum
    #: (`MCP | HTTP | A2A | AGUI`) — verified against the live botocore model rather
    #: than assumed, because a wrong value here would deploy fine and only misbehave
    #: on the wire.
    protocol = {"serverProtocol": "AGUI"}

    #: Human-paced, unlike the ground-truth runtime's 300/3600.
    #:
    #: A builder session is a person typing: 15 minutes of idle is a user thinking, not
    #: a wedged session. The gateway caps a conversation at 40 turns and 3 concurrent
    #: sessions per tenant (R10), so the spend ceiling lives there — this timeout only
    #: decides how long a warm runtime waits, and cutting it short would make a user
    #: who steps away lose their session rather than save money.
    lifecycle = {"idleRuntimeSessionTimeout": 900, "maxLifetime": 28800}

    #: ⚠️ Passed on BOTH create and update, because `update_agent_runtime` is a full
    #: REPLACE and not a merge. Omitting this on the update path silently wipes the
    #: variables the create set — and the failure is invisible while the values happen
    #: to match the defaults baked into config.py. It surfaces the first time a
    #: non-default matters, several redeploys after the one that broke it.
    #: Emitted on RUN_STARTED as `runtimeVersion`, so a consumer can tell which
    #: build actually served a turn. A session pins to a warm container and keeps
    #: running the image it started on across deploys, and `get-agent-runtime`
    #: reports the CONFIGURED version rather than the serving one — that gap cost
    #: three false "reproductions" of an already-fixed defect on 2026-08-10.
    #:
    #: Both halves earn their place: the git tag says WHICH SOURCE, the digest
    #: prefix says which artifact, and they can disagree (a `-dirty` tag, or a
    #: rebuild of the same commit). Resolved HERE rather than at module scope so
    #: `--check` still touches neither git nor the working tree.
    build_version = f"{image_tag()}@{container_uri.rsplit(':', 1)[-1][:12]}"

    environment = {
        "SKILL_BUILDER_MODEL_ID": "anthropic.claude-sonnet-5",
        "SKILL_BUILDER_AWS_REGION": REGION,
        "SKILL_BUILDER_BUILD_VERSION": build_version,
    }

    if existing:
        response = control.update_agent_runtime(
            agentRuntimeId=existing["agentRuntimeId"],
            agentRuntimeArtifact=artifact,
            networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration=protocol,
            roleArn=role_arn,
            lifecycleConfiguration=lifecycle,
            environmentVariables=environment,
        )
        print(f"[runtime] updated to version {response.get('agentRuntimeVersion')}")
        return existing["agentRuntimeArn"]

    response = control.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="AEO Conversational Skill Builder (AG-UI chat runtime)",
        agentRuntimeArtifact=artifact,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration=protocol,
        roleArn=role_arn,
        lifecycleConfiguration=lifecycle,
        environmentVariables=environment,
    )
    print(f"[runtime] created: {response['agentRuntimeArn']} ({response['status']})")
    return response["agentRuntimeArn"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="read-only inventory")
    parser.add_argument("--skip-push", action="store_true", help="do not rebuild/push")
    parser.add_argument(
        "--role-arn",
        help=(
            "use an execution role an administrator already created. Skips IAM "
            "entirely, so no IAM permission is needed here — but iam:PassRole on that "
            "role is still required by CreateAgentRuntime."
        ),
    )
    args = parser.parse_args()

    print(f"account={ACCOUNT} region={REGION} runtime={RUNTIME_NAME}\n")
    repo_uri = ensure_ecr(args.check)
    role_arn = ensure_role(args.check, args.role_arn)

    if args.check:
        # Deliberately does not resolve an image tag: --check inspects what EXISTS in
        # the account and must not depend on the local working tree.
        ensure_runtime("", role_arn or "", check=True)
        return 0

    container_uri = f"{repo_uri}:{image_tag()}" if repo_uri else ""
    if not args.skip_push and repo_uri:
        container_uri = build_and_push(repo_uri)
    elif args.skip_push and image_tag() == "unknown":
        # --skip-push deploys by TAG rather than by digest, so an unresolvable tag
        # would build a URI that cannot exist and fail deep inside CreateAgentRuntime
        # as an image-resolution error. Refuse up front instead.
        print("[image] --skip-push needs a git HEAD to name an existing tag; none found.")
        return 2

    blockers = [
        name
        for name, ok in (("ECR repository", repo_uri), ("IAM execution role", role_arn))
        if not ok
    ]
    if blockers:
        print()
        print(f"Stopping before the runtime. Blocked on: {', '.join(blockers)}.")
        print("Nothing above was left half-done and this script is idempotent, so")
        print("re-running once access is granted finishes the job.")
        return 2

    try:
        arn = ensure_runtime(container_uri, role_arn, check=False)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
            raise
        # Do NOT hardcode which action/resource to request.
        #
        # CreateAgentRuntime authorizes THREE actions whose names are not inferable
        # from the call — it implicitly creates a DEFAULT endpoint, and a workload
        # identity whose resource is not a `runtime/*` ARN at all. The sibling repo
        # hardcoded `runtime/*` here and printed the WRONG statement to request; a
        # grant on the wrong resource denies byte-for-byte identically, so the next
        # run read as "the grant never landed" and the blame went to the administrator.
        # Parse what AWS actually said instead, and say so when guessing.
        message = exc.response["Error"].get("Message", "")
        print()
        print("AccessDenied creating the runtime. AWS said:")
        print(f"  {message}")
        print()
        print("Request EXACTLY the action and resource named above — do not infer them.")
        return 3

    print()
    print(f"Runtime ARN: {arn}")
    print("Hand this to aeo-backend; it is what unblocks R2's streaming turn endpoint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
