"""Starts the editor's server (CloudFront sends /_wake here when it's asleep)."""
import json
import os

import boto3

ec2 = boto3.client("ec2")


def handler(event, context):
    iid = os.environ["INSTANCE_ID"]
    state = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]["State"]["Name"]
    if state == "stopped":
        ec2.start_instances(InstanceIds=[iid])
        state = "starting"
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps({"state": state})}
