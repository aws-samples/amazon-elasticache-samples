# Agent Powered Caching Assessment Tool

The tool looks at a running, self-managed PostgreSQL database and answers one question: would a cache (Amazon ElastiCache for Valkey) in front of it help, and if so, for which queries and which tables, and at what cost?

It reads the statistics PostgreSQL already keeps, scores every query and table with fixed formulas, has an AI model review those scores, and gives you a report with a verdict for each item, a suggested cache key and a sized, priced cache cluster.

- It reads statistics, never table rows, and it writes nothing to your database.
- PostgreSQL stays the system of record. This is a caching assessment, not a migration.
- The command-line tool is `apcat`.

> **Status: release 0.4.0.** Collection, scoring, the AI review, sizing, the report and the web console work end to end and have been run against a loaded PostgreSQL 16 database on EC2. The thresholds are reasoned defaults, not yet calibrated, so treat scores as directional. See [Limitations](#limitations).

**Contents:** [Quick start with a coding agent](#quick-start-let-a-coding-agent-run-it) · [Install and run it yourself](#install-and-run-it-yourself) · [Prepare the database](#prepare-the-database) · [Run an assessment in the console](#run-an-assessment-in-the-console) · [Run from the command line](#run-from-the-command-line) · [How it works](#how-it-works) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Limitations](#limitations) · [Security](#security) · [For developers](#for-developers) · [Uninstall](#uninstall) · [License](#license)

---

## Quick start: let a coding agent run it

Claude Code, Codex or Kiro can run the whole assessment for you. The agent installs and starts the web console, finds your databases, connects one, checks it, runs the assessment, and then either sends you to the console or talks the results through with you. It drives the same local API as the console, so everything it does shows up there, and you can do any step yourself in the console instead.

**You need:**

- macOS or Linux. Windows is not tested.
- Python 3.12 or later and [uv](https://docs.astral.sh/uv/getting-started/installation/).
- Node.js 22.12 or later, for the web console.
- SSH access to the database host with a key. The full list is under [Install and run it yourself](#install-and-run-it-yourself), and the database side under [Prepare the database](#prepare-the-database).
- Optional: AWS credentials, for the AI review (Amazon Bedrock) and the monthly cost (AWS Pricing API). Everything else works without them.

**Set up once:**

```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd amazon-elasticache-samples/tools/agent-powered-caching-assessment-tool
uv tool install --force --editable .     # puts `apcat` on your PATH, from this checkout
apcat agent install                      # the skill for Claude Code, Codex and Kiro (apcat agent uninstall removes it)
```

`--editable` runs `apcat` from this folder, so keep it; `git pull` updates it.

Two settings the agent cannot guess:

- **The SSH key.** If `ssh <host>` already works from your terminal (a key in `~/.ssh/config` or your SSH agent), there is nothing to do. Otherwise put the key in a folder and tell the tool: `apcat config set ssh_key_dir ~/apcat/keys`.
- **AWS credentials, for the AI review and the cost.** Set them in the terminal you start the agent from, for example `export AWS_PROFILE=my-profile AWS_REGION=us-east-1`. `apcat start` passes them to the API. The AI review uses `global.anthropic.claude-sonnet-5-5` by default: make sure your account can use that model in Amazon Bedrock in that region, or set another with `PGCA_BEDROCK_MODEL_ID` (see [Configuration](#configuration)).

**Then paste this into the agent:**

```text
Assess my PostgreSQL databases for caching. Run `apcat guide` and follow it: find the databases I can connect, help me pick one, run an assessment, then let me choose between the web console and discussing the results here.
```

With the skill installed, this is enough (in Claude Code, `/assess-postgres` also works):

```text
Assess my Postgres database for caching
```

**What the agent does:**

1. Installs the web console the first time, starts it with the API on 127.0.0.1, and gives you its address.
2. Lists your saved and detected databases, or probes an SSH host you name. When the SSH user can run commands as postgres (`sudo -n`), the probe lists the databases on the host, so you just pick one.
3. Offers the database user: by default it creates a read-only user `apcat_reader` for you (after you say yes; the password is made on the host and stays there), or one with another name, or an existing user.
4. Checks readiness, asks how long to measure and whether to add the AI review, runs the assessment and waits for it.
5. Summarizes the result, then opens the console or discusses it with you.

After an assessment, `apcat handoff <job>` passes it to the AWS [ElastiCache skill](https://github.com/aws/agent-toolkit-for-aws/tree/main/skills/specialized-skills/database-skills/amazon-elasticache) by writing its `.elasticache/requirements.json`, so that skill's setup and data-modeling steps start from the measured results. The agent never asks for a database password: a connection names a password file on the database host or a Secrets Manager secret. `apcat start` runs the API and the console on 127.0.0.1 only. Every command prints JSON; `apcat --help` lists them. To try it without a cloud host, [`scripts/local-pg`](scripts/local-pg/README.md) runs a disposable database host (PostgreSQL and sshd in one container, under continuous load), and [`tests/agent_eval`](tests/agent_eval/README.md) runs real Claude Code sessions through scenarios against it and checks what the agent did.

---

## Install and run it yourself

### What you need

On the machine that runs the tool (your laptop or a jump host, macOS or Linux):

| Need | Why |
|---|---|
| Python 3.12 or later | The API, collector and scoring |
| Node.js 22.12 or later | The web console |
| `ssh`, with the database host's key in `known_hosts` | The tool collects over SSH. An unknown or changed host key refuses the connection. The console can add a new host key after you confirm its fingerprint. |
| An SSH private key that can sign in to the database host | Kept in a folder you point the tool at (`SSH_KEY_DIR`) |
| AWS credentials with `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` | Only for the AI review and the assistant. Everything else works without them. |
| AWS credentials with `pricing:GetProducts` (optional) | For the monthly cost. Without it, sizing still runs and cost shows as unavailable. |

On the database host: `psql`, and PostgreSQL 14 or later (13 works with a less exact window).

### Steps

```bash
git clone https://github.com/aws-samples/amazon-elasticache-samples.git
cd amazon-elasticache-samples/tools/agent-powered-caching-assessment-tool

# Backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Console
(cd ui && npm ci)
```

Check the install with a bundled sample. This needs no database and no AWS access:

```bash
make assess            # a workload with strong candidates
make assess-negative   # a workload where every table is ruled out, each for a different reason
```

### Start the tool

Start the API in one terminal, from the repository root:

```bash
export AWS_REGION=us-east-1                  # region for Amazon Bedrock
export ARTIFACT_ROOT=$HOME/apcat/artifacts   # where assessments are kept
export SSH_KEY_DIR=$HOME/apcat/keys          # folder with the SSH key(s) for your database hosts
# export SSH_KNOWN_HOSTS=$HOME/apcat/known_hosts   # optional, default ~/.ssh/known_hosts

.venv/bin/python -m src.api.main
```

It listens on `127.0.0.1:8000` and refuses any caller that is not on the same machine.

Start the console in a second terminal:

```bash
cd ui
npm run dev
```

Open http://127.0.0.1:3000.

Pick an `ARTIFACT_ROOT` outside `/tmp` if you want to keep assessments across reboots.

---

## Prepare the database

On the database host, once. Steps 1 and 4 need someone with admin rights on PostgreSQL. Step 3 the tool can do for you.

1. **Turn on `pg_stat_statements`.** Add it to `shared_preload_libraries` in `postgresql.conf`, restart PostgreSQL, then run `CREATE EXTENSION pg_stat_statements;` in the database you want to assess. Without it, the tool runs in degraded mode: it can still score tables, but confidence is capped at 60.
2. **Turn on `track_io_timing`** (recommended). It lets the tool see time spent reading from disk.
3. **A read-only database user.** By default the tool creates one named `apcat_reader` when you add the database, after you confirm. That needs an SSH user that can run commands as postgres without a password (`sudo -n -u postgres`), `python3` on the host and PostgreSQL 14 or later. The tool runs one fixed script through sudo: it makes a random password in a file only the SSH user can read (`~/.apcat_reader_pw`), sends only the password's SCRAM hash in the SQL, and grants only what the tool needs. It never touches an existing role's password, a role with admin rights, or the server's settings.

   Without sudo, create the role by hand and keep its password in a file on the host, readable only by the SSH user (`umask 077`, one line):

   ```sql
   CREATE ROLE apcat_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
   \password apcat_reader
   ALTER ROLE apcat_reader SET default_transaction_read_only = on;
   GRANT pg_monitor TO apcat_reader;        -- statistics views and query text
   GRANT pg_read_all_data TO apcat_reader;  -- table sizes, column lists, column statistics
   ```

   `\password` in `psql` keeps the password out of the SQL and the server log.
4. **Let the role sign in with its password from `127.0.0.1`.** The tool runs `psql` on the database host itself. Many packages (Amazon Linux, RHEL and their derivatives) ship a `pg_hba.conf` that uses `ident` for `127.0.0.1`, which refuses a password sign-in. Add this line above the existing `127.0.0.1` lines, then reload with `SELECT pg_reload_conf();`:

   ```
   host  all  apcat_reader  127.0.0.1/32  scram-sha-256
   ```

   The readiness check tells you when the sign-in still fails.

---

## Run an assessment in the console

1. **Get started.** The console lists PostgreSQL databases it found on this machine. To use a remote host, probe it from the same page. The probe checks the host in stages (name lookup, port reachable, host key, SSH sign-in, shell, PostgreSQL, databases) and tells you which stage failed and what to fix. If the host key is new, it shows the fingerprint and adds it only after you confirm. When the SSH user can run commands as postgres (`sudo -n`), it lists the databases on the host (not the default `postgres` database).
2. **Add the database.** Pick it, or give the SSH host, the SSH key and the database name. For the database user, keep the default (the tool creates `apcat_reader` after you confirm), choose another name, or use an existing role with its password file or secret.
3. **Check readiness.** The tool checks the SSH connection, the database sign-in, that the SSH host is the database host, and `pg_stat_statements`, and says what to fix.
4. **Run an assessment.** Choose a collection window of 5 to 240 minutes, during normal or peak load. The run takes at least that long. Turn on **AI review** to have the model review the scores (it is off by default). Sizing and pricing are on by default.
5. **Read the report.** The headline verdict for the workload, then every query and table with its verdict, scores, the evidence behind them, a suggested cache key and what would make a cached entry wrong. The Sizing & cost section shows the memory, node type, node count and monthly On-Demand cost for what was recommended.
6. **Ask the assistant.** The drawer on every page answers questions about the report on screen ("why was this table ruled out?") and cites the report's own numbers. It can also start, rerun or cancel assessments, after you confirm the exact request.

Each database page lists its assessments. You can rerun an assessment on its saved collection, for example with AI review turned on, without collecting again.

---

## Run from the command line

The scripts run the same four phases as the console.

```bash
# 1. Collect, on the database host (collection only)
PGPASSWORD=... .venv/bin/python scripts/collect_local.py --backend binary \
    --user apcat_reader --database mydb -o collection.json

# 2. Score and size it. Drop --no-llm for the AI review, drop --no-price for cost.
.venv/bin/python scripts/run_assessment_v2.py --file collection.json --no-llm --no-price -o analysis.json

# 3. Build the report: one self-contained HTML file
.venv/bin/python scripts/build_report_v2.py --collection collection.json \
    --analysis analysis.json -o report.html
```

Each script lists its options with `--help`.

---

## How it works

```
COLLECT ──> ANALYZE ──> SIZE & COST ──> REPORT
              │
              ├─ 1. Score (fixed formulas, no AI)
              └─ 2. Review (AI model, optional)
```

| Phase | What happens |
|---|---|
| Collect | Over SSH, the tool runs `psql` on the database host and reads `pg_stat_statements`, the table and database statistics, the catalog and column statistics. It reads them at the start and the end of the window and keeps the difference, so the numbers describe that window. It samples locks and host CPU, memory and swap every 15 seconds. |
| Analyze | Every query and table gets a score from fixed formulas. Then, if AI review is on, a model reviews the scores and gives the final verdicts. |
| Size & cost | For what was recommended: the memory the cache needs, the node type and count, and the On-Demand price from the AWS Pricing API. Node-based Valkey only. |
| Report | One HTML file, also shown in the console. |

### Step 1: can it be cached at all?

A SQL parser reads every query first. A query that writes, calls `random()`, depends on a clock finer than an hour (like `now()`), or is a session statement is **Not cacheable**. That is final: it gets no score and nothing can recommend it.

The rest get a cache type:

- **OBJECT:** rows looked up by a key, such as a customer by `customer_id`. Only writes to those rows make a cached entry wrong.
- **QUERY:** a whole result, such as a search that joins two tables. Any write to any table it reads makes it wrong.

### Step 2: the deterministic score

Each item is scored on three pillars, each from 0 to 100, and then loses points for named reasons not to cache. The same data always gives the same score.

```
confidence = 0.35 x Access + 0.40 x Pressure + 0.25 x Efficiency - penalties
```

| Pillar | The question | What it reads | How it scores |
|---|---|---|---|
| **Access** (35%) | Is it read far more than it is written, and read often? | Calls per query, and measured writes on every table the query reads | Full marks at 90% or more reads and 10 or more reads a second |
| **Pressure** (40%) | Is PostgreSQL working hard to answer it? | Buffer hit ratio, disk read time, mean execution time, share of all database time, temp spills, repeated full scans, host CPU, memory and swap | Points for each sign of strain. Slow calls, a large share of database time and reads from disk score most. Memory points are halved when `shared_buffers` is under 25% of the database size, and the report says to fix that first. |
| **Efficiency** (25%) | If a cache were there, how often would it answer? | Calls in the window (N), the number of distinct answers the query can produce (K, from the table statistics), result size | An estimated hit rate from N and K. Full marks at an 80% hit rate. Plus how much time each hit saves: nothing at 1 ms, full marks at 100 ms. |
| **Disqualifiers** (points off) | Is there a reason not to cache? | Write share, server headroom, locks | Heavy writes cost up to 55 points, busy-but-fast items 10, lock contention 15. One rule rules an item out completely: it is served from memory, takes under 2 ms, never spills, the server has headroom and the item uses under 5% of it. |

Pressure has the most weight because it answers "why now?". A query can be a perfect fit for a cache and still not need one if the database answers it from memory in a millisecond.

When a number cannot be computed, it is marked "not scored" and the other parts carry its weight. A missing number is never treated as a zero.

The score maps to a band:

| Band | Confidence |
|---|---|
| Strong | 70 to 100 |
| Moderate | 50 to 69 |
| Weak | 30 to 49 |
| Not recommended | below 30, or ruled out |

"Recommended" means Strong or Moderate.

**Example.** A product search query in the marketplace test workload, at normal load: 100% reads at 54 calls a second (Access 100), 48% of all database time at 9.2 ms a call (Pressure 46), and 81,468 calls over 2,400 distinct search terms, a 97% hit rate (Efficiency 87). No penalties. `0.35 x 100 + 0.40 x 46 + 0.25 x 87 = 75`, **Strong**.

The tool also looks for **bundles**: a query that is Not cacheable as written, but that the application could compute from cached rows, because every table it reads changes each row at most once an hour.

Every threshold is defined in one place, `CacheScoringThresholds` in [`src/tools/analysis/caching_scoring.py`](src/tools/analysis/caching_scoring.py).

### Step 3: the AI review

Thresholds cannot capture everything an experienced engineer would weigh. With AI review on, a model on Amazon Bedrock (Anthropic Claude, by default `global.anthropic.claude-sonnet-5-5`) reviews the scores and gives the final verdict for each item.

- **What it gets.** A short task prompt and 13 read-only tools over the scored data: scores, the evidence behind them, write rates, the host, bundles and the thresholds. It pulls what it needs.
- **What it may do.** Give a different verdict from the band when the figures support it, and explain why. Estimate how many distinct requests the application really sends, for range, `IN`, `LIKE` and `OFFSET` queries where the statistics cannot know. Suggest a cache key, what invalidates it, which columns to store, and a cheaper fix such as an index.
- **What it may not do.** Change a score, cite a number it did not get from a tool, or recommend anything that is ruled out or Not cacheable.
- **How its answer is checked.** Its answer is a structured object, and every verdict is checked in code. A verdict with an unknown id, no reasoning, or a missing required field is dropped, and that item keeps its band. When it estimates distinct requests, the hit rate and score are recomputed from its estimate. Tables in one bundle must get verdicts within one band of each other, or the model gets one turn to correct them.
- **When it is off or fails.** The bands become the verdicts, and the report says so (`deterministic_fallback`).

| Situation | Final verdict |
|---|---|
| Ruled out or Not cacheable | Not recommended, always |
| The model gave a valid verdict | The model's verdict |
| The model's verdict failed a check | The band |
| AI review off, or the model failed | The band |

The scores are reproducible. The model's wording and judgement can vary between runs on the same data.

---

## Configuration

Environment variables read by the API when it starts:

| Variable | Default | What it does |
|---|---|---|
| `ARTIFACT_ROOT` | `./artifacts` | Where assessments, saved databases and the audit log are kept |
| `SSH_KEY_DIR` | not set | Folder with SSH private keys. The console offers the keys in it by file name. |
| `SSH_KNOWN_HOSTS` | `~/.ssh/known_hosts` | Host keys the tool trusts. Keys added from the console are appended here. |
| `AWS_REGION` | `us-east-1` | Region for Amazon Bedrock |
| `PGCA_BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-5-5` | Model for the AI review. It must be enabled in your account and region. |
| `PGCA_CHAT_MODEL_ID` | same as above | Model for the assistant |
| `PGCA_BEDROCK_READ_TIMEOUT_SEC` | 600 | Bedrock read timeout |
| `API_PORT` | 8000 | API port. Set `APCAT_API=http://127.0.0.1:<port>` for `npm run dev` too. |
| `MAX_RUNNING_JOBS`, `MAX_QUEUED_JOBS` | 4, 20 | How many assessments run and wait at once |
| `JOB_RETENTION_DAYS` | off | Delete finished assessments after this many days |
| `AUDIT_LOG_PATH` | `audit.log` in `ARTIFACT_ROOT` | Audit log location |

When `apcat start` runs the API, it passes on the environment it was started from, and adds the SSH key folder, known_hosts file and artifact folder set with `apcat config`.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Probe fails at **host_key** | The host is not in `known_hosts`. Compare the fingerprint the console shows with the host's own, then trust it. A key that changed is never replaced automatically. Find out why it changed. |
| Probe fails at **sign_in** | The host refused the SSH key. Pick the right key from `SSH_KEY_DIR`, and check the SSH user. |
| Probe fails at **reachable** | Port 22 is closed to you. Check the security group or firewall. The console shows the public IP it connected from. |
| Readiness says `pg_stat_statements` is missing | Load it in `shared_preload_libraries`, restart PostgreSQL and run `CREATE EXTENSION pg_stat_statements;`, or accept degraded mode. |
| `ModelRequired: the model was required but not used` | **Require the model** was on and the model could not be used. The text after it says why: no AWS credentials, the model is not enabled, or the AWS SDK could not load. Fix it and rerun the assessment on its saved collection. |
| `Unable to load data for: endpoints` | The Python environment changed under a running API, for example because a tool rebuilt `.venv`. Reinstall with `pip install -e ".[dev]"` and restart the API. The project uses `pip` and `.venv`. `uv run` inside the repository rebuilds `.venv`. |
| An assessment says "interrupted" | The API stopped while it ran. Jobs run inside the API process. Start a new one. |
| The console shows nothing | Check the API is up: `curl http://127.0.0.1:8000/health`. |

---

## Limitations

Read these before showing output to anyone.

| Limitation | What it means |
|---|---|
| Thresholds are not calibrated | Scores and bands are directional. Release 1.0.0 is reserved for after calibration. |
| The hit rate is a floor | It assumes every key is equally popular and ignores entries that outlive the window. Real hit rates are usually higher. |
| Averages hide peaks | Rates are averages over the window you pick. Pick a period of normal or peak load. |
| Sizing counts the window's traffic, not the working set | The real working set needs a pilot. |
| Cost is On-Demand node list price | Node-based Valkey only. No Serverless, reserved nodes or data transfer. Not a quote. |
| The AI review is not deterministic | Wording and verdicts can move between runs on the same data. |
| `pg_stat_bgwriter` signals target PostgreSQL 13 to 16 | PostgreSQL 17 split the view. |
| The API has no authentication | It answers callers on the same machine only. Add authentication before exposing it. |

---

## Security

- **Read only.** The tool reads statistics and catalog views through a read-only role. It never reads table rows and never writes to the database.
- **The password stays on the database host.** It is read from a file there by `psql` and never travels through the API, the collector or the logs.
- **Host keys are checked.** An unknown host key is added only after you confirm its fingerprint, and a changed key is never replaced.
- **Query text can hold literal values.** Treat assessments, reports and saved assistant conversations as sensitive.
- **Local only.** The API and the console bind to `127.0.0.1` and refuse other machines. That is a network check, not authentication. Do not put it behind a reverse proxy.
- **AWS calls use the API server's credentials.** Whoever runs the API owns the Bedrock and Pricing access and its cost.

### What leaves your machine

| Where | When | What |
|---|---|---|
| Your database host, over SSH | Probe, check, role setup, collection | Fixed commands and read-only SQL. Results come back to your machine. |
| Amazon Bedrock, in your AWS account and region | AI review on, or a question to the assistant | The collected statistics, including query text, which can hold literal values. Billed to your account per run and per question. |
| AWS Pricing API | Sizing with pricing on (the default) | Node types and the region only. No workload data. |
| `checkip.amazonaws.com` | A probe whose SSH port does not answer | A request for this machine's public IP, to put in the hint |
| `raw.githubusercontent.com` | The assistant answers a general caching question | Downloads guides from the public AWS ElastiCache skill at a pinned commit. Sends nothing. |

The tool creates no AWS resources and sends no telemetry.

---

## For developers

### Tests

```bash
.venv/bin/pytest -q                      # backend: unit and contract tests
(cd ui && npm test && npm run build)     # console: tests, type check and build
make lint typecheck security             # ruff, mypy, bandit
```

`tests/manual/` holds scripts that need a live database. They are run by hand.

### Layout

```
src/
├── version.py          the one release number
├── contracts/          data contracts for collection, analysis, API and discovery
├── tools/              database access over SSH, discovery and host probe, scoring, report, AWS
├── agents/             collector, analysis (scoring + AI review), sizing, chat assistant
├── orchestrator/       runs COLLECT, ANALYZE, SIZE, REPORT
└── api/                FastAPI app, loopback only
ui/                     React, TypeScript and Cloudscape console (see ui/README.md)
scripts/                command-line collect, assess, size, report, load generator
tests/                  unit, contract, manual, fixtures
```

### API reference

With the API running, http://127.0.0.1:8000/docs lists every route, its request and response schema and its errors.

## Uninstall

```bash
apcat stop                        # the API and console apcat started
apcat agent uninstall             # the files apcat agent install wrote
uv tool uninstall pg-cache-advisor
```

Then delete this folder. Assessments, saved databases and the audit log are in its `artifacts/` folder unless you set `ARTIFACT_ROOT` or `apcat config set artifact_root`.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file. To report a security issue, see [CONTRIBUTING](../../CONTRIBUTING.md#security-issue-notifications).
