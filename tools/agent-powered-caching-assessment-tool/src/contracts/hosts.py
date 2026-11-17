"""
SSH host probe contract 1.0: can the tool reach this host, and does PostgreSQL run there?

A probe goes through fixed stages and stops at the first that fails. Every message is fixed
text chosen by the server; the text of an ssh error is never returned. When the SSH user may
run `sudo -n -u postgres`, the last stage lists the databases with read-only catalog queries
as postgres; otherwise it is a warning and nothing signs in. Spec: .kiro/specs/ssh-host-probe.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.discovery import DiscoveredServer

HOST_PROBE_CONTRACT_VERSION = "1.1"

StageName = Literal["resolve", "reachable", "host_key", "sign_in", "shell", "postgresql",
                    "databases"]
StageStatus = Literal["pass", "fail", "warn", "skipped"]
HostKeyState = Literal["known", "unknown", "changed", "unavailable", "not_checked"]
FINGERPRINT_PATTERN = r"^SHA256:[A-Za-z0-9+/]{43}$"


class HostTarget(BaseModel):
    """The SSH host to probe or trust."""

    model_config = ConfigDict(extra="forbid")

    ssh_target: str = Field(max_length=320, description="user@host, a host, or a name from "
                            "ssh_config")
    ssh_port: int = Field(default=22, ge=1, le=65535)
    identity_file: str | None = Field(default=None, max_length=128,
                                      description="A key file name inside SSH_KEY_DIR. Omit "
                                      "for the key the SSH config names")


class ProbeStage(BaseModel):
    """One stage of a probe."""

    model_config = ConfigDict(extra="forbid")

    name: StageName
    status: StageStatus
    message: str
    hint: str | None = Field(default=None, description="What to do about a fail or warn")


class HostKeyInfo(BaseModel):
    """The host's SSH key, as scanned, against the server's known_hosts."""

    model_config = ConfigDict(extra="forbid")

    state: HostKeyState = "not_checked"
    key_type: str | None = None
    fingerprint: str | None = Field(default=None, description="SHA256 fingerprint of the "
                                    "key to trust, in the form ssh-keygen -l prints")


class HostProbeResult(BaseModel):
    """What a probe found."""

    model_config = ConfigDict(extra="forbid")

    version: str = HOST_PROBE_CONTRACT_VERSION
    ok: bool = Field(description="True when SSH works and a shell runs. PostgreSQL not "
                     "running is a warning, not a failure")
    ssh_target: str
    ssh_port: int
    resolved_host: str | None = Field(default=None, description="The host name ssh connects "
                                      "to, after the SSH config")
    resolved_user: str | None = None
    via_jump_host: bool = False
    stages: list[ProbeStage]
    host_key: HostKeyInfo = Field(default_factory=HostKeyInfo)
    public_ip: str | None = Field(default=None, description="This machine's public IP, only "
                                  "when the SSH port did not answer")
    clusters: list[DiscoveredServer] = Field(default_factory=list)
    postgres_access: Literal["sudo", "no_sudo", "failed", "not_checked"] = Field(
        default="not_checked", description="sudo: the SSH user can run commands as postgres, "
        "so the databases are listed and the tool can create its read-only user. no_sudo: it "
        "cannot; give the database name and an existing user. not_checked: no instance found")
    databases: list[DiscoveredServer] = Field(
        default_factory=list, description="With sudo, every database in each instance, largest "
        "first: name, size and pg_stat_statements state. Each can be added like a cluster find")
    duration_seconds: float


class HostTrustRequest(HostTarget):
    """Trust a host's key after checking its fingerprint."""

    fingerprint: str = Field(pattern=FINGERPRINT_PATTERN, description="The fingerprint the "
                             "user checked, from the probe")


class HostTrustResult(BaseModel):
    """What trusting a host did."""

    model_config = ConfigDict(extra="forbid")

    trusted: bool
    outcome: Literal["added", "already_known", "mismatch", "changed", "unavailable"]
    message: str
    fingerprint: str | None = None


class SshKeyInfo(BaseModel):
    """One key in SSH_KEY_DIR. Never its contents."""

    model_config = ConfigDict(extra="forbid")

    name: str
    key_type: str | None = None
    fingerprint: str | None = None
    usable: bool
    problem: str | None = Field(default=None, description="Why ssh would not use it")


class SshKeyList(BaseModel):
    """The keys the operator made available."""

    model_config = ConfigDict(extra="forbid")

    configured: bool = Field(description="False when SSH_KEY_DIR is not set")
    items: list[SshKeyInfo] = Field(default_factory=list)


class HostSuggestion(BaseModel):
    """A host the user might want to probe."""

    model_config = ConfigDict(extra="forbid")

    ssh_target: str
    ssh_port: int | None = None
    identity_file: str | None = None
    label: str
    source: Literal["saved_database", "recent_probe", "ssh_config"]
    code_host: bool = Field(default=False, description="The name looks like a git or code "
                            "host, not a database host")
    last_result: str | None = Field(default=None, description="For a recent probe: ok, or "
                                    "the stage that failed")
    last_probed_at: str | None = None


class HostSuggestionList(BaseModel):
    """Hosts to suggest, saved databases first."""

    model_config = ConfigDict(extra="forbid")

    items: list[HostSuggestion]
