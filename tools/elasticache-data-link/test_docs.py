#!/usr/bin/env python3
"""Check the documentation against the code.

WHY THIS EXISTS. The README documented a `config.yaml` shape that the validator
rejected with 11 problems, told the reader to copy an example that failed with 19,
and promised a `target.endpoint: auto` feature that no longer existed. A reader
following it could not succeed, and nothing anywhere noticed — because
documentation is the one part of a repo with no tests.

Every check here corresponds to a mistake that was actually in these files:

  1. a YAML example in the docs that the validator rejects
     (the README's own link example had THREE such errors while being written:
      child_table instead of parent_table, a transform for a table missing from
      `tables`, and a rename of a column `columns` did not select)
  2. an `ecdl` command or flag in the docs that does not exist
  3. a relative link to a file that is not there

Run:  python3 test_docs.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(ROOT, 'ecdl-cli', 'ecdl')

DOCS = (
    'README.md',
    'docs/prerequisites.md',
    'docs/one-pager.md',
    'docs/benchmark-results.md',
    'docs/getting-started.md',
    'docs/operations.md',
    'ecdl-cli/examples/README.md',
)

FAILURES = []


def fail(message):
    FAILURES.append(message)


def read(path):
    return io.open(os.path.join(ROOT, path), encoding='utf-8').read()


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run_cli(*args):
    """Run this repository's own CLI in-process, the way `python ecdl ...` would.

    In-process rather than as a subprocess: nothing is executed, so there is
    no command line to inject into, and the docs check needs no interpreter path.
    """
    import contextlib
    import runpy
    out, err = io.StringIO(), io.StringIO()
    argv, cwd = sys.argv, os.getcwd()
    code = 0
    try:
        sys.argv = [CLI] + list(args)
        os.chdir(ROOT)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runpy.run_path(CLI, run_name='__main__')
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if not isinstance(exc.code, (int, type(None))):
            err.write(str(exc.code))
    except Exception as exc:  # a crash in the CLI is a failed check, not a crashed test
        code = 1
        err.write('%s: %s' % (type(exc).__name__, exc))
    finally:
        sys.argv = argv
        os.chdir(cwd)
    return _Result(code, out.getvalue(), err.getvalue())


# ── 1. every YAML link example in the docs must validate ───────────────────
def yaml_blocks(text):
    """Fenced ```yaml blocks that look like a whole link config.

    A block is only treated as a full link when it has `name:` at the left
    margin -- the docs also show FRAGMENTS (a transform on its own, a couple of
    source fields), and demanding that a fragment validate as a whole link would
    make this test report failures that are not real problems.
    """
    for block in re.findall(r'```yaml\n(.*?)```', text, re.S):
        if re.search(r'^name:', block, re.M):
            yield block


def test_documented_link_examples_validate():
    checked = 0
    for path in DOCS:
        for i, block in enumerate(yaml_blocks(read(path))):
            tmp = '/tmp/_docs_check_%d.yaml' % os.getpid()
            io.open(tmp, 'w', encoding='utf-8').write(block)
            out = run_cli('validate', tmp)
            checked += 1
            if out.returncode != 0:
                detail = (out.stdout + out.stderr).strip()
                fail('%s: yaml example %d does not validate:\n      %s'
                     % (path, i + 1, detail.replace('\n', '\n      ')))
            os.unlink(tmp)
    if not checked:
        fail('no link examples found in the docs at all -- the extractor is '
             'probably broken, which would make this whole test pass without checking anything')
    print('  %d documented link example(s) validate' % checked)


# ── 2. every ecdl command and flag in the docs must exist ──────────────────
def test_documented_commands_exist():
    referenced = {}
    for path in DOCS:
        for m in re.finditer(r'(?:^|[\s`])\.?/?ecdl ([a-z-]+)([^\n`|]*)',
                             read(path), re.M):
            sub, rest = m.group(1), m.group(2)
            flags = set(re.findall(r'(--[a-z-]+)', rest))
            referenced.setdefault(sub, set()).update(flags)
    if not referenced:
        fail('no ecdl commands found in the docs -- extractor broken')
    for sub in sorted(referenced):
        out = run_cli(sub, '--help')
        help_text = out.stdout + out.stderr
        if out.returncode != 0:
            fail('docs reference `ecdl %s`, which does not exist' % sub)
            continue
        for flag in sorted(referenced[sub]):
            if flag not in help_text:
                fail('docs reference `ecdl %s %s`, but that flag does not exist'
                     % (sub, flag))
    print('  %d ecdl command(s) referenced, all exist' % len(referenced))


# ── 3. relative links must resolve ─────────────────────────────────────────
def test_relative_links_resolve():
    count = 0
    for path in DOCS:
        base = os.path.dirname(os.path.join(ROOT, path))
        for target in re.findall(r'\]\((?!https?:|mailto:)([^)#]+)', read(path)):
            count += 1
            if not os.path.exists(os.path.normpath(os.path.join(base, target))):
                fail('%s links to %s, which does not exist' % (path, target))
    print('  %d relative link(s) resolve' % count)


def test_current_example_validates():
    path = 'ecdl-cli/examples/checkout-cache/link.yaml'
    out = run_cli('validate', path)
    if out.returncode != 0:
        fail('the example the docs tell you to start from does not validate:\n'
             '      %s' % (out.stdout + out.stderr).strip())
    print('  the shipped link example validates')


# ── 5. claims that would be wrong if a feature came back ───────────────────
def test_docs_do_not_teach_removed_settings():
    """The old shape is REJECTED by name, so documenting it teaches a config the
    tool refuses. These strings are the ones that were actually in the README."""
    removed = {
        'endpoint: auto': 'ECDL no longer creates the target cache',
        'snapshot_mode': "replaced by the link's `mode`",
        'child_table': 'denormalize now names parent_table, and the direction '
                       'is inverted',
    }
    # Explanation is rarely on the same line as the thing being explained: a
    # migration table puts the old value in one cell and "inverted" in another, and
    # a before/after code block labels the old half with a comment above it. So the
    # window is the surrounding lines, not the line.
    # The same name where it is still a setting: the advanced override of
    # Debezium's snapshot mode is current (and the schema-history runbook uses
    # it), only source.snapshot_mode was removed.
    current = {'snapshot_mode': 'advanced.debezium.snapshot_mode'}
    WINDOW = 4
    MARKERS = ('old', 'older', 'earlier', 'former', 'previous', 'superseded',
               'no longer', 'removed', 'gone', 'replaced', 'was ', 'used to',
               'inverted', 'not a rename', 'will not deploy', 'rejected')

    for path in DOCS:
        lines = read(path).splitlines()
        for needle, why in removed.items():
            for i, line in enumerate(lines):
                if needle not in line.replace(current.get(needle, needle * 2), ''):
                    continue
                context = ' '.join(
                    lines[max(0, i - WINDOW):i + WINDOW + 1]).lower()
                if not any(word in context for word in MARKERS):
                    fail('%s presents removed setting %r as current (%s):\n'
                         '      %s' % (path, needle, why, line.strip()))
    print('  no removed setting is presented as current')


if __name__ == '__main__':
    print('documentation checks')
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print()
    if FAILURES:
        print('%d PROBLEM(S):' % len(FAILURES))
        for problem in FAILURES:
            print('  - %s' % problem)
        sys.exit(1)
    print('ok: documentation matches the code')
