#!/usr/bin/env python3
"""AWS setup for the club web app. See ../docs/aws.md.

    ./deploy.sh                  deploys from your computer, with your own AWS sign-in

Settings (cdk.json "context", or -c name=value): instanceType, dataGb,
idleMinutes, keepRendersMinutes, keepUploadsHours, maxUploadGb, budgetEmail, monthlyBudgetUsd, image.
"""
import os

import aws_cdk as cdk

from racebox_infra.server_stack import ServerStack

app = cdk.App()
region = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=region)

ServerStack(app, "RaceboxServer", env=env,
            description="RaceBox Overlay club web app: editor server, sign-in, renders")
app.synth()
