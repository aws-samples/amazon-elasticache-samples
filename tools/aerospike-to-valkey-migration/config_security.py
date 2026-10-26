#!/usr/bin/env python3
"""Trust checks for the migration mapping configuration.

Addresses threat T4 (config file tampering / redirection).

``mapping_config.yaml`` is a security-relevant file: it supplies the Aerospike
source host, the Valkey destination host, the TLS policy and the key-naming
templates. An attacker who can modify it (or swap it via a symlink, or drop a
lookalike into a world-writable working directory) can:

  * redirect every migrated record to an endpoint they control (exfiltration),
  * downgrade or disable TLS on either leg (plaintext transport),
  * rewrite ``key_format`` so records collapse onto, or overwrite, keys of
    their choosing in the destination cluster,
  * inject hostile regular expressions into the field-pattern matcher.

This module makes the config a *verified* input instead of a trusted one:

  1. **Path safety** - the config must be a regular file, not a symlink, and
     must live in a directory that is not writable by other users.
  2. **Ownership / permissions** - the file must be owned by the invoking user
     (or root) and must not be group- or world-writable.
  3. **Integrity pinning** - an expected SHA-256 may be supplied explicitly, via
     the ``MAPPING_CONFIG_SHA256`` environment variable, or via a
     ``<config>.sha256`` sidecar file. A mismatch is a hard failure. Set
     ``MAPPING_CONFIG_REQUIRE_PIN=1`` to refuse to run unpinned.
  4. **Schema validation** - every security-relevant value is checked against an
     allowlist before it can influence a connection or a destination key.

The resolved digest is returned so callers can record it in their audit trail.

Also usable as a CLI so shell scripts share the same verified loader:

    python3 config_security.py --emit-shell mapping_config.yaml   # CFG_* vars
    python3 config_security.py --pin        mapping_config.yaml   # write sidecar
    python3 config_security.py --fingerprint mapping_config.yaml  # print sha256
"""

import hashlib
import os
import re
import shlex
import stat
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - surfaced by callers
    yaml = None


class ConfigSecurityError(Exception):
    """Raised when the mapping configuration cannot be trusted."""


# --------------------------------------------------------------------------
# Allowlists. Anything outside these sets is rejected rather than passed
# through to a connection, a Valkey command or a destination key name.
# --------------------------------------------------------------------------

ALLOWED_TOP_LEVEL_KEYS = {
    'connection_settings',
    'transport_security',
    'global_settings',
    'data_type_mappings',
    'namespace_mappings',
    'field_mappings',
    'detection_rules',
    'fallback_strategies',
    'performance_settings',
    'validation_rules',
}

ALLOWED_KEY_PLACEHOLDERS = {'namespace', 'set', 'primary_key', 'bin_name'}

ALLOWED_VALKEY_COMMANDS = {
    'SET', 'RPUSH', 'HSET', 'JSON.SET', 'GEOADD', 'PFADD',
}

ALLOWED_VALKEY_TYPES = {
    'string', 'native', 'list', 'hash', 'json', 'geospatial', 'hyperloglog',
}

ALLOWED_STRATEGIES = {
    'direct', 'with_bin_name', 'separate_keys', 'json_document', 'hybrid',
}

ALLOWED_LOG_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}

# Literal characters permitted in a key template, outside of placeholders.
# Deliberately excludes whitespace, quotes, slashes and shell metacharacters so
# a tampered template cannot be used to build surprising key names.
_KEY_LITERAL_RE = re.compile(r'^[A-Za-z0-9._:\-]*$')

# A single {placeholder} occurrence. Attribute/index access (e.g.
# "{namespace.__class__}") is rejected because the name must match the
# allowlist exactly.
_PLACEHOLDER_RE = re.compile(r'\{([^{}]*)\}')

# Hostnames / IPs only - no schemes, credentials, ports, paths or spaces.
_HOST_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$')

# Wildcard field patterns are compiled into regexes by the mapping engine, so
# restrict them to literal characters plus '*' and '?'.
_FIELD_PATTERN_RE = re.compile(r'^[A-Za-z0-9_*?.\-]+$')

# Filesystem paths accepted from the config (CA bundles, key/cert files).
_PATH_RE = re.compile(r'^[A-Za-z0-9._/\-]*$')


# --------------------------------------------------------------------------
# Path, ownership and permission checks
# --------------------------------------------------------------------------

def _describe(path: str) -> str:
    return path


def resolve_config_path(config_file: str) -> str:
    """Return an absolute path to the config, rejecting unsafe path shapes.

    A symlinked config is refused outright: a symlink is the easiest way to
    silently point the migration at a different file than the operator audited.
    """
    if not config_file:
        raise ConfigSecurityError("No mapping configuration path was provided.")

    path = os.path.abspath(config_file)

    if os.path.islink(path):
        raise ConfigSecurityError(
            f"Mapping config {_describe(path)} is a symbolic link. Refusing to "
            f"follow it - point --mapping-config at the real file so the file "
            f"that is audited is the file that is used (threat T4)."
        )

    if not os.path.exists(path):
        raise ConfigSecurityError(f"Mapping config {_describe(path)} does not exist.")

    if not os.path.isfile(path):
        raise ConfigSecurityError(
            f"Mapping config {_describe(path)} is not a regular file."
        )

    return path


def _unsafe_perms_allowed() -> bool:
    return os.environ.get('MAPPING_CONFIG_ALLOW_UNSAFE_PERMS') == '1'


def _check_owner_and_mode(path: str, kind: str, warnings: List[str]) -> None:
    info = os.stat(path)
    euid = os.geteuid()

    if info.st_uid not in (euid, 0):
        msg = (
            f"{kind} {_describe(path)} is owned by uid {info.st_uid}, which is "
            f"neither the current user (uid {euid}) nor root. A file owned by "
            f"another account can be rewritten to redirect this migration."
        )
        if _unsafe_perms_allowed():
            warnings.append(msg)
        else:
            raise ConfigSecurityError(msg)

    # Group- or world-writable means someone other than the owner can tamper.
    # A world-writable *directory* is tolerable only when it is sticky (/tmp).
    writable_bits = info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if writable_bits:
        sticky_dir = os.path.isdir(path) and bool(info.st_mode & stat.S_ISVTX)
        if not sticky_dir:
            who = []
            if info.st_mode & stat.S_IWGRP:
                who.append("group")
            if info.st_mode & stat.S_IWOTH:
                who.append("world")
            msg = (
                f"{kind} {_describe(path)} is {'- and '.join(who)}-writable "
                f"(mode {oct(info.st_mode & 0o777)}). Tighten it with "
                f"'chmod go-w {path}' so it cannot be tampered with."
            )
            if _unsafe_perms_allowed():
                warnings.append(msg)
            else:
                raise ConfigSecurityError(msg)


def check_file_trust(path: str) -> List[str]:
    """Verify ownership and permissions of the config and its directory.

    Returns a list of warning strings (non-empty only when the operator has set
    MAPPING_CONFIG_ALLOW_UNSAFE_PERMS=1 to downgrade failures to warnings).
    """
    warnings: List[str] = []
    _check_owner_and_mode(path, "Mapping config", warnings)
    # A writable parent directory lets an attacker replace the file wholesale,
    # which defeats any check on the file itself.
    _check_owner_and_mode(os.path.dirname(path) or '.', "Mapping config directory", warnings)
    return warnings


# --------------------------------------------------------------------------
# Integrity pinning
# --------------------------------------------------------------------------

def fingerprint(path: str) -> str:
    """Return the SHA-256 hex digest of a file."""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


_DIGEST_RE = re.compile(r'^[0-9a-fA-F]{64}$')


def _read_sidecar_digest(path: str) -> Optional[str]:
    sidecar = path + '.sha256'
    if not os.path.isfile(sidecar) or os.path.islink(sidecar):
        return None
    # The pin file is only meaningful if it is as protected as the config.
    _check_owner_and_mode(sidecar, "Mapping config pin file", [])
    with open(sidecar, 'r') as handle:
        # Accept both a bare digest and 'sha256sum' output ("<digest>  <name>").
        fields = handle.read().strip().split()
    token = fields[0] if fields else ''
    if not _DIGEST_RE.match(token):
        raise ConfigSecurityError(
            f"Pin file {sidecar} does not contain a valid SHA-256 digest."
        )
    return token.lower()


def resolve_expected_digest(path: str, explicit: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Resolve the expected digest and where it came from."""
    if explicit:
        if not _DIGEST_RE.match(explicit.strip()):
            raise ConfigSecurityError(
                "The supplied mapping-config SHA-256 is not a 64-character hex digest."
            )
        return explicit.strip().lower(), 'command line'

    env_digest = os.environ.get('MAPPING_CONFIG_SHA256', '').strip()
    if env_digest:
        if not _DIGEST_RE.match(env_digest):
            raise ConfigSecurityError(
                "MAPPING_CONFIG_SHA256 is not a 64-character hex digest."
            )
        return env_digest.lower(), 'MAPPING_CONFIG_SHA256'

    sidecar = _read_sidecar_digest(path)
    if sidecar:
        return sidecar, os.path.basename(path) + '.sha256'

    return None, 'unpinned'


def verify_integrity(path: str, explicit: Optional[str] = None) -> Tuple[str, List[str]]:
    """Compare the config's digest against its pin. Returns (digest, warnings)."""
    actual = fingerprint(path)
    expected, source = resolve_expected_digest(path, explicit)
    warnings: List[str] = []

    if expected is None:
        if os.environ.get('MAPPING_CONFIG_REQUIRE_PIN') == '1':
            raise ConfigSecurityError(
                f"MAPPING_CONFIG_REQUIRE_PIN=1 but no expected digest was found for "
                f"{_describe(path)}. Pin it with "
                f"'python3 config_security.py --pin {path}' or pass "
                f"--mapping-config-sha256 {actual}."
            )
        warnings.append(
            f"Mapping config {_describe(path)} is not integrity-pinned "
            f"(sha256={actual}). Pin it with "
            f"'python3 config_security.py --pin {path}' so later tampering is detected."
        )
    elif expected != actual:
        raise ConfigSecurityError(
            f"Mapping config integrity check FAILED for {_describe(path)}.\n"
            f"  expected sha256: {expected} (from {source})\n"
            f"  actual   sha256: {actual}\n"
            f"The configuration has changed since it was pinned. Review the diff "
            f"before re-pinning - it controls the destination host, the TLS policy "
            f"and the destination key names (threat T4)."
        )

    return actual, warnings


def write_pin(path: str) -> str:
    """Write a 0600 ``<config>.sha256`` pin file. Returns the digest."""
    digest = fingerprint(path)
    sidecar = path + '.sha256'
    fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(f"{digest}  {os.path.basename(path)}\n")
    return digest


# --------------------------------------------------------------------------
# Schema validation
# --------------------------------------------------------------------------

def _validate_key_template(template: Any, name: str, required: set) -> None:
    if not isinstance(template, str) or not template:
        raise ConfigSecurityError(
            f"global_settings.{name} must be a non-empty string."
        )
    if len(template) > 256:
        raise ConfigSecurityError(f"global_settings.{name} is unreasonably long.")

    found = set()
    for placeholder in _PLACEHOLDER_RE.findall(template):
        if placeholder not in ALLOWED_KEY_PLACEHOLDERS:
            raise ConfigSecurityError(
                f"global_settings.{name} contains disallowed placeholder "
                f"'{{{placeholder}}}'. Allowed placeholders: "
                f"{sorted(ALLOWED_KEY_PLACEHOLDERS)}."
            )
        found.add(placeholder)

    # Literal text between placeholders must be inert.
    literals = _PLACEHOLDER_RE.sub('', template)
    if '{' in literals or '}' in literals:
        raise ConfigSecurityError(
            f"global_settings.{name} has unbalanced braces."
        )
    if not _KEY_LITERAL_RE.match(literals):
        raise ConfigSecurityError(
            f"global_settings.{name} contains disallowed literal characters. "
            f"Only letters, digits and '. _ : -' may surround placeholders."
        )

    missing = required - found
    if missing:
        raise ConfigSecurityError(
            f"global_settings.{name} must include {sorted('{%s}' % m for m in missing)}. "
            f"Without it, distinct source records would collide onto the same "
            f"destination key and overwrite each other (threat T4)."
        )


def _validate_mapping_block(block: Any, where: str) -> None:
    """Recursively check every valkey_type / valkey_command against allowlists."""
    if isinstance(block, dict):
        vtype = block.get('valkey_type')
        if vtype is not None:
            if not isinstance(vtype, str) or vtype not in ALLOWED_VALKEY_TYPES:
                raise ConfigSecurityError(
                    f"{where}.valkey_type '{vtype}' is not one of "
                    f"{sorted(ALLOWED_VALKEY_TYPES)}."
                )
        vcmd = block.get('valkey_command')
        if vcmd is not None:
            if not isinstance(vcmd, str) or vcmd.upper() not in ALLOWED_VALKEY_COMMANDS:
                raise ConfigSecurityError(
                    f"{where}.valkey_command '{vcmd}' is not one of "
                    f"{sorted(ALLOWED_VALKEY_COMMANDS)}. A tampered config must "
                    f"not be able to issue arbitrary commands to the destination "
                    f"cluster (threat T4)."
                )
        prefix = block.get('prefix')
        if prefix is not None and (not isinstance(prefix, str) or not _KEY_LITERAL_RE.match(prefix)):
            raise ConfigSecurityError(f"{where}.prefix contains disallowed characters.")
        for child_name, child in block.items():
            if child_name in ('valkey_type', 'valkey_command', 'prefix'):
                continue
            _validate_mapping_block(child, f"{where}.{child_name}")


def _validate_host(value: Any, where: str) -> None:
    if not isinstance(value, str) or not _HOST_RE.match(value):
        raise ConfigSecurityError(
            f"{where} '{value}' is not a bare hostname or IP address. "
            f"Schemes, credentials, ports and paths are not permitted."
        )


def _validate_port(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not (1 <= value <= 65535):
        raise ConfigSecurityError(f"{where} '{value}' is not a valid TCP port.")


def _validate_bool(value: Any, where: str) -> None:
    if not isinstance(value, bool):
        raise ConfigSecurityError(
            f"{where} must be a YAML boolean (true/false), got {value!r}. "
            f"Ambiguous values are rejected so a TLS setting cannot be quietly "
            f"reinterpreted."
        )


def _validate_path(value: Any, where: str) -> None:
    if value in (None, ''):
        return
    if not isinstance(value, str) or not _PATH_RE.match(value):
        raise ConfigSecurityError(
            f"{where} '{value}' is not an acceptable filesystem path."
        )


def _validate_patterns(patterns: Any, where: str) -> None:
    if patterns is None:
        return
    if isinstance(patterns, dict):
        names = list(patterns.keys())
    elif isinstance(patterns, list):
        names = patterns
    else:
        raise ConfigSecurityError(f"{where} must be a list or mapping of patterns.")
    for pattern in names:
        if not isinstance(pattern, str) or not _FIELD_PATTERN_RE.match(pattern):
            raise ConfigSecurityError(
                f"{where} contains disallowed pattern {pattern!r}. Patterns are "
                f"compiled into regular expressions, so only letters, digits, "
                f"'_', '.', '-', '*' and '?' are permitted."
            )


def validate_schema(config: Any) -> None:
    """Validate every security-relevant value in the mapping configuration."""
    if not isinstance(config, dict):
        raise ConfigSecurityError("Mapping config must be a YAML mapping at the top level.")

    unknown = set(config) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ConfigSecurityError(
            f"Mapping config contains unknown top-level section(s): {sorted(unknown)}. "
            f"Known sections: {sorted(ALLOWED_TOP_LEVEL_KEYS)}."
        )

    # --- connection settings: the exfiltration-critical values ---
    conn = config.get('connection_settings') or {}
    if not isinstance(conn, dict):
        raise ConfigSecurityError("connection_settings must be a mapping.")

    aero = conn.get('aerospike') or {}
    if aero:
        if not isinstance(aero, dict):
            raise ConfigSecurityError("connection_settings.aerospike must be a mapping.")
        if 'host' in aero:
            _validate_host(aero['host'], 'connection_settings.aerospike.host')
        if 'port' in aero:
            _validate_port(aero['port'], 'connection_settings.aerospike.port')
        if 'tls' in aero:
            _validate_bool(aero['tls'], 'connection_settings.aerospike.tls')
        for field in ('tls_ca_cert', 'tls_certfile', 'tls_keyfile'):
            if field in aero:
                _validate_path(aero[field], f'connection_settings.aerospike.{field}')
        if aero.get('tls_name') not in (None, ''):
            _validate_host(aero['tls_name'], 'connection_settings.aerospike.tls_name')

    valkey = conn.get('valkey') or {}
    if valkey:
        if not isinstance(valkey, dict):
            raise ConfigSecurityError("connection_settings.valkey must be a mapping.")
        if 'host' in valkey:
            _validate_host(valkey['host'], 'connection_settings.valkey.host')
        if 'port' in valkey:
            _validate_port(valkey['port'], 'connection_settings.valkey.port')
        if 'tls' in valkey:
            _validate_bool(valkey['tls'], 'connection_settings.valkey.tls')
        if 'tls_ca_cert' in valkey:
            _validate_path(valkey['tls_ca_cert'], 'connection_settings.valkey.tls_ca_cert')

    transport = config.get('transport_security') or {}
    if transport:
        if not isinstance(transport, dict):
            raise ConfigSecurityError("transport_security must be a mapping.")
        if 'allow_plaintext' in transport:
            _validate_bool(transport['allow_plaintext'], 'transport_security.allow_plaintext')

    # --- key naming: the overwrite/redirection-critical values ---
    global_settings = config.get('global_settings') or {}
    if not isinstance(global_settings, dict):
        raise ConfigSecurityError("global_settings must be a mapping.")

    _validate_key_template(
        global_settings.get('key_format'), 'key_format', {'primary_key'}
    )
    _validate_key_template(
        global_settings.get('multi_key_format'), 'multi_key_format',
        {'primary_key', 'bin_name'},
    )

    for field in ('single_bin_strategy', 'multi_bin_strategy'):
        value = global_settings.get(field)
        if value is not None and value not in ALLOWED_STRATEGIES:
            raise ConfigSecurityError(
                f"global_settings.{field} '{value}' is not one of {sorted(ALLOWED_STRATEGIES)}."
            )

    level = global_settings.get('mapping_log_level')
    if level is not None and str(level).upper() not in ALLOWED_LOG_LEVELS:
        raise ConfigSecurityError(
            f"global_settings.mapping_log_level '{level}' is not one of "
            f"{sorted(ALLOWED_LOG_LEVELS)}."
        )

    if 'preserve_ttl' in global_settings:
        _validate_bool(global_settings['preserve_ttl'], 'global_settings.preserve_ttl')

    # --- commands and types anywhere in the mapping tree ---
    for section in ('data_type_mappings', 'field_mappings', 'namespace_mappings'):
        if section in config:
            _validate_mapping_block(config[section], section)

    for ns_name, ns_config in (config.get('namespace_mappings') or {}).items():
        if not isinstance(ns_config, dict):
            continue
        if ns_config.get('strategy') and ns_config['strategy'] not in ALLOWED_STRATEGIES:
            raise ConfigSecurityError(
                f"namespace_mappings.{ns_name}.strategy "
                f"'{ns_config['strategy']}' is not one of {sorted(ALLOWED_STRATEGIES)}."
            )
        for set_name, set_config in (ns_config.get('sets') or {}).items():
            if not isinstance(set_config, dict):
                continue
            for field in ('strategy', 'single_bin_strategy', 'multi_bin_strategy'):
                value = set_config.get(field)
                if value is not None and value not in ALLOWED_STRATEGIES:
                    raise ConfigSecurityError(
                        f"namespace_mappings.{ns_name}.sets.{set_name}.{field} "
                        f"'{value}' is not one of {sorted(ALLOWED_STRATEGIES)}."
                    )

    # --- patterns compiled into regexes ---
    _validate_patterns(
        (config.get('field_mappings') or {}).get('field_name_patterns'),
        'field_mappings.field_name_patterns',
    )
    detection = config.get('detection_rules') or {}
    if not isinstance(detection, dict):
        raise ConfigSecurityError("detection_rules must be a mapping.")
    for rule_name, rule in detection.items():
        if isinstance(rule, dict):
            _validate_patterns(
                rule.get('field_name_patterns'),
                f'detection_rules.{rule_name}.field_name_patterns',
            )

    # Sections the mapping engine dereferences unconditionally.
    for required in ('data_type_mappings', 'detection_rules'):
        if required not in config:
            raise ConfigSecurityError(f"Mapping config is missing required section '{required}'.")


# --------------------------------------------------------------------------
# Public loader
# --------------------------------------------------------------------------

def load_trusted_config(
    config_file: str,
    expected_sha256: Optional[str] = None,
) -> Tuple[Dict[str, Any], str, str, List[str]]:
    """Load the mapping config only if it passes every trust check.

    Returns ``(config, resolved_path, sha256, warnings)`` and raises
    :class:`ConfigSecurityError` on any failure.
    """
    if yaml is None:
        raise ConfigSecurityError("PyYAML is required to load the mapping configuration.")

    path = resolve_config_path(config_file)
    warnings = check_file_trust(path)
    digest, integrity_warnings = verify_integrity(path, expected_sha256)
    warnings.extend(integrity_warnings)

    with open(path, 'r') as handle:
        config = yaml.safe_load(handle)

    validate_schema(config)
    warnings.extend(_transport_warnings(config))
    return config, path, digest, warnings


# Aerospike listens for TLS on a different port than plaintext, so a config that
# turns TLS on but leaves the plaintext port in place will simply fail to
# connect. That is fail-closed, not a data leak, but it is easy to misread as
# "TLS is broken" - so name the likely cause up front.
_PLAINTEXT_AEROSPIKE_PORT = 3000
_TLS_AEROSPIKE_PORT = 4333


def _transport_warnings(config: Dict[str, Any]) -> List[str]:
    conn = (config or {}).get('connection_settings') or {}
    aero = conn.get('aerospike') or {}
    if aero.get('tls') and aero.get('port') == _PLAINTEXT_AEROSPIKE_PORT:
        return [
            f"connection_settings.aerospike.tls is true but port is "
            f"{_PLAINTEXT_AEROSPIKE_PORT}, the plaintext service port. The TLS "
            f"handshake will fail. Set port to {_TLS_AEROSPIKE_PORT} (or whichever "
            f"TLS service port your cluster uses), or remove the port key to let "
            f"it be inferred from the tls setting."
        ]
    return []


def plaintext_allowed_by_config(config: Dict[str, Any]) -> bool:
    """Whether the config explicitly opts in to unencrypted transport."""
    transport = config.get('transport_security') or {}
    return bool(transport.get('allow_plaintext', False))


# --------------------------------------------------------------------------
# CLI - lets shell scripts reuse the same verified loader
# --------------------------------------------------------------------------

def _emit_shell(config: Dict[str, Any], path: str, digest: str) -> None:
    conn = config.get('connection_settings') or {}
    aero = conn.get('aerospike') or {}
    valkey = conn.get('valkey') or {}

    aero_tls = bool(aero.get('tls', False))
    valkey_tls = bool(valkey.get('tls', True))

    values = {
        'CFG_PATH': path,
        'CFG_SHA256': digest,
        'CFG_AEROSPIKE_HOST': str(aero.get('host', '127.0.0.1')),
        'CFG_AEROSPIKE_PORT': str(aero.get('port', 4333 if aero_tls else 3000)),
        'CFG_AEROSPIKE_TLS': 'true' if aero_tls else 'false',
        'CFG_AEROSPIKE_TLS_CA_CERT': str(aero.get('tls_ca_cert') or ''),
        'CFG_AEROSPIKE_TLS_NAME': str(aero.get('tls_name') or ''),
        'CFG_VALKEY_HOST': str(valkey.get('host', '')),
        'CFG_VALKEY_PORT': str(valkey.get('port', 6379)),
        'CFG_VALKEY_TLS': 'true' if valkey_tls else 'false',
        'CFG_VALKEY_TLS_CA_CERT': str(valkey.get('tls_ca_cert') or ''),
        'CFG_ALLOW_PLAINTEXT': 'true' if plaintext_allowed_by_config(config) else 'false',
    }
    for name, value in values.items():
        print(f"{name}={shlex.quote(value)}")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Verify and inspect the migration mapping configuration (threat T4)."
    )
    parser.add_argument('config', help="Path to mapping_config.yaml")
    parser.add_argument('--mapping-config-sha256',
                        help="Expected SHA-256 of the config file.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--emit-shell', action='store_true',
                      help="Print verified settings as shell assignments (CFG_*).")
    mode.add_argument('--fingerprint', action='store_true',
                      help="Print the config's SHA-256 and exit.")
    mode.add_argument('--pin', action='store_true',
                      help="Write a 0600 <config>.sha256 pin file for the current contents.")
    mode.add_argument('--check', action='store_true',
                      help="Run all trust checks and report (default).")
    args = parser.parse_args(argv)

    try:
        if args.fingerprint:
            print(fingerprint(resolve_config_path(args.config)))
            return 0

        if args.pin:
            path = resolve_config_path(args.config)
            check_file_trust(path)
            validate_schema(yaml.safe_load(open(path)))
            digest = write_pin(path)
            print(f"Pinned {path}\n  sha256 {digest}\n  written to {path}.sha256 (mode 0600)",
                  file=sys.stderr)
            return 0

        config, path, digest, warnings = load_trusted_config(
            args.config, args.mapping_config_sha256
        )

        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)

        if args.emit_shell:
            _emit_shell(config, path, digest)
        else:
            print(f"OK: {path}\n  sha256 {digest}", file=sys.stderr)
        return 0

    except ConfigSecurityError as exc:
        print(f"CONFIG TRUST CHECK FAILED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # malformed YAML, unreadable file, ...
        print(f"CONFIG TRUST CHECK FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
