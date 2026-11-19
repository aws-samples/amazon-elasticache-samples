"""
The shape of every value a live collection sends to `ssh` or to a shell.

A live job puts the SSH target, the user, the key name and the password file on the `ssh`
command line, and the database host, port, database name and password file in a command run
on the database host. A value with a quote, a space, a newline or a `$` would be run as shell,
and one starting with a dash could be read as an `ssh` option. These checks refuse such values
before anything is called. They are applied at the API (a 422), in preflight, and again in
`collect()` where the command is built, so a caller that skips the API is covered too.

Each check is a full match (not `$`, which also accepts a trailing newline). The messages name
the field and never echo the value.

Spec: .kiro/specs/ssh-collection (SSH-5).
"""

import re

from src.contracts.collector_input import CollectorInput
from src.tools.aws import secrets
from src.tools.database import ssh_values

_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,254}")
_DATABASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")


def target_problems(inp: CollectorInput) -> list[str]:
    """What is wrong with the shape of the values a live collection would use.

    Args:
        inp: The collector input.

    Returns:
        One sentence per problem, naming the field and not echoing its value. Empty
        when every value is acceptable.
    """
    problems: list[str] = []

    def check(field: str, value: str | None, ok, required: bool = True):
        if value is None:
            if required:
                problems.append(f"{field} is required")
        elif not ok(value):
            problems.append(f"{field} is not in an accepted form")

    check("cluster_endpoint", inp.cluster_endpoint, lambda v: bool(_HOST.fullmatch(v)))
    check("database_name", inp.database_name, lambda v: bool(_DATABASE.fullmatch(v)))
    if not 1 <= inp.port <= 65535:
        problems.append("port must be between 1 and 65535")
    live = inp.live_config
    check("live_config.ssh_target", live.ssh_target if live else None, ssh_values.target_ok)
    check("live_config.db_user", live.db_user if live else None, ssh_values.db_user_ok)
    check("live_config.password_file", live.password_file if live else None,
          ssh_values.password_file_ok, required=False)
    arn = live.password_secret_arn if live else None
    check("live_config.password_secret_arn", arn, secrets.arn_ok, required=False)
    if live is not None and (live.password_file is None) == (arn is None):
        problems.append("give exactly one of live_config.password_file and "
                        "live_config.password_secret_arn")
    if arn is not None and secrets.arn_ok(arn):
        try:
            secrets.check_allowed(arn)
        except secrets.SecretError as e:
            problems.append(f"live_config.password_secret_arn: {secrets.MESSAGES[e.kind][0]}")
    check("live_config.identity_file", live.identity_file if live else None,
          ssh_values.key_name_ok, required=False)
    if live is not None and not 1 <= live.ssh_port <= 65535:
        problems.append("live_config.ssh_port must be between 1 and 65535")
    return problems
