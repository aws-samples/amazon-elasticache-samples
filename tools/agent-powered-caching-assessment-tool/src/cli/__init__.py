"""`apcat`: the command line a coding agent (Claude Code, Codex, Kiro) drives.

A thin client of the local API: every command is one or two API calls and prints
JSON. The API keeps the state (saved databases, jobs, artifacts), so whatever an agent
does here shows up in the web console straight away. Spec: .kiro/specs/agent-skill.
"""
