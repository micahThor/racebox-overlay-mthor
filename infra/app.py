#!/usr/bin/env python3
"""AWS setup for the club web app. See ../docs/aws.md.

    cdk deploy RaceboxGithub     once, from your computer: lets GitHub Actions deploy
    cdk deploy RaceboxServer     the app itself (GitHub Actions does this on every push)

Settings (cdk.json "context", or -c name=value): instanceType, dataGb,
idleMinutes, keepRendersDays, maxUploadGb, adminEmail, budgetEmail,
monthlyBudgetUsd, githubRepo, githubBranch, githubOidcProviderArn.
"""
import os

import aws_cdk as cdk

from racebox_infra.github_stack import GithubStack
from racebox_infra.server_stack import ServerStack

app = cdk.App()
region = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=region)

ServerStack(app, "RaceboxServer", env=env,
            description="RaceBox Overlay club web app: editor server, sign-in, renders")
GithubStack(app, "RaceboxGithub", env=env,
            description="Lets GitHub Actions deploy the RaceBox Overlay web app")
app.synth()
