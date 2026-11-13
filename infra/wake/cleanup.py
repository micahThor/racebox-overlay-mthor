"""Deletes rendered videos from the bucket once they're older than the limit
(runs every 5 minutes, so nothing outlives KEEP_MINUTES)."""
import os
import time

import boto3

s3 = boto3.client("s3")


def handler(event, context):
    bucket = os.environ["BUCKET"]
    cutoff = time.time() - (float(os.environ["KEEP_MINUTES"]) - 5) * 60
    gone = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="outputs/"):
        old = [{"Key": o["Key"]} for o in page.get("Contents", []) if o["LastModified"].timestamp() < cutoff]
        for i in range(0, len(old), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": old[i:i + 1000], "Quiet": True})
            gone += len(old[i:i + 1000])
    return {"deleted": gone}
