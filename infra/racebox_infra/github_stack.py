"""Lets GitHub Actions deploy (deploy this one once, from your own computer).

GitHub signs in to AWS with a short-lived token (OpenID Connect), only from
this repository's main branch, so no AWS keys are stored in GitHub. The role
can only: run CDK deployments (through the roles `cdk bootstrap` made), push
the app's image, and restart the app on the server.

Some new AWS accounts aren't allowed to add GitHub as a sign-in provider. For
those, `-c githubAuth=keys` makes a deploy user instead, with the same
permissions; you store its access key as GitHub secrets.
"""
from aws_cdk import CfnOutput, Duration, Stack, aws_iam as iam
from constructs import Construct

from .server_stack import REPO_NAME, TAG, ctx

GITHUB = "token.actions.githubusercontent.com"


class GithubStack(Stack):
    def __init__(self, scope: Construct, cid: str, **kw) -> None:
        super().__init__(scope, cid, **kw)
        repo = ctx(self, "githubRepo", "micahThor/racebox-overlay-mthor")
        branch = ctx(self, "githubBranch", "main")

        statements = self.deploy_permissions()

        if ctx(self, "githubAuth") == "keys":
            # for accounts that can't add GitHub as a sign-in provider (some new AWS
            # accounts can't): a user whose access key you create and store as GitHub secrets
            user = iam.User(self, "DeployUser", user_name="racebox-overlay-github-deploy")
            for st in statements:
                user.add_to_policy(st)
            CfnOutput(self, "DeployUserName", value=user.user_name,
                      description="Create an access key for this user and store it as GitHub secrets")
            return

        existing = ctx(self, "githubOidcProviderArn")   # an account can only have one GitHub provider
        provider_arn = existing or iam.OpenIdConnectProvider(
            self, "GitHubOidc", url=f"https://{GITHUB}", client_ids=["sts.amazonaws.com"]
        ).open_id_connect_provider_arn

        role = iam.Role(
            self, "DeployRole", role_name="racebox-overlay-github-deploy",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(provider_arn, conditions={
                "StringEquals": {f"{GITHUB}:aud": "sts.amazonaws.com",
                                 f"{GITHUB}:sub": f"repo:{repo}:ref:refs/heads/{branch}"}}),
            description=f"GitHub Actions in {repo} ({branch}) deploys the RaceBox Overlay web app")
        for st in statements:
            role.add_to_policy(st)
        CfnOutput(self, "DeployRoleArn", value=role.role_arn,
                  description="Put this in the GitHub repository variable AWS_ROLE_ARN")

    def deploy_permissions(self) -> list:
        """Run CDK deployments, push the app's image, restart the app on the server."""
        return [
            # cdk deploy: hands the real work to the roles `cdk bootstrap` created
            iam.PolicyStatement(actions=["sts:AssumeRole"], resources=[f"arn:aws:iam::{self.account}:role/cdk-*"]),
            # push the app's image
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"]),
            iam.PolicyStatement(
                actions=["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
                         "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
                         "ecr:PutImage", "ecr:UploadLayerPart"],
                resources=[f"arn:aws:ecr:{self.region}:{self.account}:repository/{REPO_NAME}"]),
            # restart the app on the server so it picks up the new image
            iam.PolicyStatement(actions=["ec2:DescribeInstances"], resources=["*"]),
            iam.PolicyStatement(
                actions=["ssm:SendCommand"], resources=[f"arn:aws:ec2:{self.region}:{self.account}:instance/*"],
                conditions={"StringEquals": {f"ssm:resourceTag/{TAG[0]}": TAG[1]}}),
            iam.PolicyStatement(actions=["ssm:SendCommand"],
                                resources=[f"arn:aws:ssm:{self.region}::document/AWS-RunShellScript"]),
            iam.PolicyStatement(actions=["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"],
                                resources=["*"]),
        ]
