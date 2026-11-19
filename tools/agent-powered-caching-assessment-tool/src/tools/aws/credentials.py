"""
AWS Credential Manager

Handles same-account (ECS task role) and cross-account (AssumeRole) access.
All AWS tools use this to get a boto3 session.
"""

import logging

import boto3

logger = logging.getLogger(__name__)


class AWSCredentialManager:
    """Manages AWS credentials transparently for same-account and cross-account."""

    def __init__(
        self,
        region: str,
        role_arn: str | None = None,
        external_id: str | None = None,
    ):
        self.region = region
        self._role_arn = role_arn
        self._external_id = external_id
        self._session: boto3.Session | None = None

    @property
    def mode(self) -> str:
        return "cross-account" if self._role_arn else "same-account"

    def get_session(self) -> boto3.Session:
        if self._session:
            return self._session

        if self._role_arn:
            self._session = self._assume_role()
        else:
            self._session = boto3.Session(region_name=self.region)

        return self._session

    def _assume_role(self) -> boto3.Session:
        sts = boto3.client("sts", region_name=self.region)
        params = {
            "RoleArn": self._role_arn,
            "RoleSessionName": "ModernizerCollector",
            "DurationSeconds": 3600,
        }
        if self._external_id:
            params["ExternalId"] = self._external_id

        creds = sts.assume_role(**params)["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.region,
        )

    def client(self, service: str):
        """Shortcut to get a boto3 client."""
        return self.get_session().client(service)
