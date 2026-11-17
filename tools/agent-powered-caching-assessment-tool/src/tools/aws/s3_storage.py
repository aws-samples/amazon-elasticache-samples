"""
S3 Storage Manager

Path convention:
  s3://pg-cache-advisor-{account_id}/{host_name}/{database_name}/{job_id}/{phase}/artifact.json

Optional in v1: the local orchestrator writes to the filesystem instead, since
deployment is out of v1 scope. This module exists for the deployed path and for
sharing assessment artifacts.

SECURITY TODO (inherited from upstream, not yet fixed):
ensure_bucket() creates the bucket with no explicit encryption, versioning,
public-access block, or bucket policy. Assessment artifacts contain schema names
and query text, so this MUST be hardened before any non-local use:
  - BlockPublicAcls / IgnorePublicAcls / BlockPublicPolicy / RestrictPublicBuckets
  - ServerSideEncryptionConfiguration (SSE-S3 at minimum, SSE-KMS preferred)
  - Versioning enabled
  - A bucket policy denying non-TLS requests
"""

import json
import logging
from typing import Any

from src.tools.aws.credentials import AWSCredentialManager

logger = logging.getLogger(__name__)

BUCKET_PREFIX = "pg-cache-advisor"

# The three pipeline phases, replacing the upstream migration stages
# ("collector", "analysis", "referee", "schema-design").
AGENT_FOLDERS = ["collect", "analyze", "recommend"]


def get_account_id(cred_mgr: AWSCredentialManager) -> str:
    sts = cred_mgr.client("sts")
    return str(sts.get_caller_identity()["Account"])


def ensure_bucket(cred_mgr: AWSCredentialManager, account_id: str | None = None) -> str:
    if not account_id:
        account_id = get_account_id(cred_mgr)
    bucket_name = f"{BUCKET_PREFIX}-{account_id}"
    s3 = cred_mgr.client("s3")
    try:
        s3.head_bucket(Bucket=bucket_name)
        logger.info("Bucket %s already exists", bucket_name)
    except Exception:
        region = cred_mgr.region
        params: dict[str, Any] = {"Bucket": bucket_name}
        if region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**params)
        logger.info("Created bucket %s", bucket_name)
    return bucket_name


def cluster_name_from_endpoint(endpoint: str) -> str:
    """Extract cluster/instance name from RDS endpoint.
    e.g. 'mydb.cluster-abc123.us-east-1.rds.amazonaws.com' → 'mydb'
    """
    return endpoint.split(".")[0]


def init_storage(
    cred_mgr: AWSCredentialManager, cluster_endpoint: str, database_name: str, job_id: str
) -> dict[str, str]:
    """
    Init storage for a job.
    Path: {cluster_name}/{database_name}/{job_id}/{agent_name}/
    """
    account_id = get_account_id(cred_mgr)
    bucket = ensure_bucket(cred_mgr, account_id)
    cluster_name = cluster_name_from_endpoint(cluster_endpoint)
    prefix = f"{cluster_name}/{database_name}/{job_id}/"

    result = {"bucket": bucket, "prefix": prefix, "cluster_name": cluster_name}
    for folder in AGENT_FOLDERS:
        result[f"{folder.replace('-', '_')}_prefix"] = f"{prefix}{folder}/"
    return result


def save_json(cred_mgr: AWSCredentialManager, bucket: str, key: str, data: Any) -> str:
    s3 = cred_mgr.client("s3")
    body = json.dumps(data, default=str, indent=2)
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/json")
    return f"s3://{bucket}/{key}"


def load_json(cred_mgr: AWSCredentialManager, bucket: str, key: str) -> Any:
    s3 = cred_mgr.client("s3")
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode())
