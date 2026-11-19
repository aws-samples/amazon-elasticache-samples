"""
Database discovery contract 1.0: PostgreSQL servers the tool could assess.

Discovery reads names, hosts, ports and versions. It never reads a password, never makes a
database connection, and never returns a full process command line. Spec:
.kiro/specs/database-discovery.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DISCOVERY_CONTRACT_VERSION = "1.1"

DiscoverySource = Literal["local_server", "ssh_config", "pg_service", "earlier_assessment",
                          "host_probe"]
LOCAL_SOURCES: tuple[DiscoverySource, ...] = ("local_server", "ssh_config", "pg_service",
                                              "earlier_assessment")
SourceStatus = Literal["ok", "unavailable", "error", "skipped"]


class DiscoveredServer(BaseModel):
    """One thing the user could pick to assess: a server, an SSH host, a service, a past target."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable id for this entry within a result")
    source: DiscoverySource
    kind: Literal["local", "ssh", "service"] = Field(
        description="How the tool would reach it: on this machine, over SSH, or a pg_service entry")
    label: str = Field(description="Plain text for a list")
    ssh_target: str | None = Field(default=None, description="SSH alias or host")
    ssh_port: int | None = None
    ssh_user: str | None = None
    identity_file: str | None = Field(default=None, description="For a host probe find, the "
                                      "key name in SSH_KEY_DIR the probe signed in with")
    host: str | None = Field(default=None, description="PostgreSQL host as psql there sees it")
    port: int | None = None
    database: str | None = None
    version: str | None = Field(default=None, description="Major version, when visible")
    data_directory: str | None = None
    owner: str | None = Field(default=None, description="OS user running the server")
    listen_addresses: list[str] = Field(default_factory=list)
    runs: int | None = Field(default=None, description="Earlier assessments of this target")
    last_assessed_at: str | None = None
    last_verdict: str | None = None
    connected: bool = Field(default=False, description="Always false: no database connection "
                            "is made by discovery")
    size_mb: float | None = Field(default=None, description="For a database a host probe "
                                  "listed as postgres: its size")
    pg_stat_statements: Literal["ready", "not_installed", "not_loaded"] | None = Field(
        default=None, description="For a listed database: ready, not_installed (CREATE "
        "EXTENSION needed in it), or not_loaded (a restart is needed). None when not known")
    postgres_access: Literal["sudo", "no_sudo"] | None = Field(
        default=None, description="For a host probe find: whether the SSH user could run "
        "commands as postgres, which lets the tool list databases and create its read-only user")
    notes: list[str] = Field(default_factory=list)


class SourceReport(BaseModel):
    """How one source went."""

    model_config = ConfigDict(extra="forbid")

    source: DiscoverySource
    status: SourceStatus
    found: int = 0
    detail: str = ""


class DiscoveryResult(BaseModel):
    """Everything discovery found, and how each source went."""

    model_config = ConfigDict(extra="forbid")

    version: str = DISCOVERY_CONTRACT_VERSION
    discovered_at: str
    probed_host: str | None = Field(default=None, description="The SSH host probed, if any")
    servers: list[DiscoveredServer] = Field(default_factory=list)
    sources: list[SourceReport] = Field(default_factory=list)
