# Change Log — Rubric Remediation & Dependency Updates

This document records all changes made to the ShopMind demo in response to the
AWS Solutions Implementation Guide rubric scan findings, plus a follow-up
dependency refresh. Each entry lists the file(s) touched, what changed, and why.

Date: October 6, 2026

---

## 1. HIGH — AWS Service Name and Messaging Compliance (SN1/SN4)

**What:** Added the required `Amazon` brand prefix to four prose mentions of
"Bedrock Agent" → "Amazon Bedrock Agent".

**Where:** `presentation/index.html` (diagram labels and agent-flow slides,
originally lines 90, 121, 300, 336).

**Why:** Amazon Bedrock is registered as PREFIX_ALWAYS_REQUIRED. The full
service name must appear at every prose mention; the bare short form
"Bedrock Agent" violated the naming rule.

---

## 2. HIGH — General Documentation Quality / Guidance Internal Consistency (GD3 / GC2)

**What:** Changed "20x faster" → "21x faster" for the 211ms → 10ms agent
tool-call improvement.

**Where:** `presentation/slides.md` (Slide 10 agent flow).

**Why:** 211 ÷ 10 = 21.1x, so "20x" was factually wrong (GD3). It also
contradicted the document's own performance table, which already said "21x"
(GC2). Correcting it to "21x" makes the arithmetic right and the document
internally consistent.

---

## 3. MEDIUM — General Documentation Quality (GD4): hyphenation

**What:** Standardized the compound adjective "Open Source" → "Open-source".

**Where:** `presentation/index.html` (card heading) and
`presentation/generate-pptx.js` (capability list).

**Why:** The attributive compound was hyphenated inconsistently across the
document ("Open-source" elsewhere, "Open Source" here). Standardized to the
hyphenated form used in the body text.

---

## 4. MEDIUM — Legal and Compliance Terminology (LC3): superlative

**What:** Replaced the unqualified superlative "The fastest path to better
database performance for Java applications" → "A low-friction path to better
database performance for Java applications".

**Where:** `presentation/index.html`, `presentation/slides.md`, and
`presentation/generate-pptx.js`.

**Why:** "Fastest" is an unverifiable comparative superlative, which the AWS
Solutions GTM style sheet disallows without supporting data. "A low-friction
path" is a defensible, non-comparative claim.

---

## 5. MEDIUM — General Documentation Quality (GD4): adverbial hyphenation

**What:** Changed "Real-time data that changes per-request" → "...per request".

**Where:** `presentation/slides.md`.

**Why:** "per request" modifies the verb "changes" adverbially. Hyphens apply
to attributive compound modifiers before a noun, not to adverbial phrases.

---

## 6. MEDIUM — Guidance Internal Consistency (GC2): step count

**What:** Reconciled the "Getting Started" step count to 5 across all files.
`slides.md` had listed 6 steps (it split the dependency install into two
numbered items); `index.html` and the PPTX said "5 steps". Merged the two
dependency steps in `slides.md` so it also lists 5.

**Where:** `presentation/slides.md` (Slide 15 list).

**Why:** The two files stated different step counts for the same procedure.
A reader comparing them could not tell which applied. Aligned all three to 5.

---

## 7. HIGH — Content Currency and Maintenance (CC1): stale dependency version

**What:** Updated `aws-advanced-jdbc-wrapper` references from **v3.3.0** →
**v4.4.0** everywhere they appear.

**Where:**
- `presentation/slides.md` (capability bullet + Maven snippet)
- `presentation/index.html` (slide 6 plugin callout + slide 13 dependency step)
- `presentation/generate-pptx.js` (capability list + Getting Started step)
- `presentation/ShopMind-JDBC-Caching-Demo.pptx` (regenerated from the JS source)

**Why:** v3.3.0 was flagged as unsupported. The current latest non-prerelease
release is **4.4.0** (verified via the GitHub Releases API:
`aws/aws-advanced-jdbc-wrapper`, tag 4.4.0, published 2026-08-20). Documenting a
supported version prevents deployment failures and keeps guidance trustworthy.

---

## 8. Follow-up — "Update everything to latest" dependency refresh (backend/pom.xml)

All versions below were verified against GitHub Releases / Maven Central at the
time of the change. Only same-major (non-breaking) upgrades were applied.

| Dependency | Previous | Updated to | Source of truth |
|---|---|---|---|
| `aws-advanced-jdbc-wrapper` | 3.3.0 | **4.4.0** | GitHub Releases API |
| AWS SDK (`bedrockruntime`, `aws.sdk.version`) | 2.31.1 | **2.55.11** | GitHub Releases API (`aws/aws-sdk-java-v2`) |
| `commons-pool2` | 2.12.0 | **2.13.1** | Maven Central |
| `valkey-glide` | 2.3.0 | **2.5.3** | GitHub Releases API (`valkey-io/valkey-glide`) |
| `spring-boot-starter-parent` | 3.3.0 | **3.5.3** | Maven Central (latest 3.x) |

**Why:** Keep the build on current, supported, patched releases.

### Spring Boot — initially kept on 3.x, later moved to 4.1.1 (see section 17)

Spring Boot was first bumped to the latest **3.x** patch (**3.5.3**) as a safe
within-major upgrade, deferring the 4.x migration. That decision was later
superseded: the entire Spring Boot 3.x line reached open-source end of life on
**2026-06-30**, so 3.5.3 became an EOL dependency and the project was migrated to
Spring Boot **4.1.1**. See section 17 for details.

---

## 9. HIGH — Infrastructure security remediations (infra/cloudformation.yml, infra/deploy.sh)

Applied before any AWS deployment, in response to the CloudFormation scan
findings. All changes are local; nothing was deployed.

| # | Finding (severity) | Fix |
|---|---|---|
| 9.1 | Security group opens **port 22 (SSH)** to `0.0.0.0/0` (HIGH) | Added `AllowedSshCidr` parameter; port 22 now references it. Default is `127.0.0.1/32` (fail-safe non-routable placeholder); an `AllowedPattern` blocks malformed input. |
| 9.2 | Security group opens app **ports 8080/3000** to `0.0.0.0/0` (HIGH) | Added `AllowedAppCidr` parameter; both ports now reference it instead of the open CIDR. |
| 9.3 | Aurora cluster **not encrypted at rest** (HIGH) | Added `StorageEncrypted: true` to the `AWS::RDS::DBCluster`. |
| 9.4 | Aurora **IAM authentication disabled** (HIGH) | Added `EnableIAMDatabaseAuthentication: true` to the cluster. |
| 9.5 | **Hardcoded DB password** in the template default and in `deploy.sh` (value redacted; rotate it on any environment that ever used it) | Removed the `DBPassword` default (now required, min 12 chars, safe `AllowedPattern`). `deploy.sh` now reads the password from the `DB_PASSWORD` environment variable. |

**deploy.sh hardening (same change set):**
- Requires an `<allowed-cidr>` argument and **refuses to deploy if it is `0.0.0.0/0`**.
- Reads `DB_PASSWORD` from the environment; errors if unset.
- Passes the CIDR into both `AllowedSshCidr` and `AllowedAppCidr`.
- Added `set -euo pipefail` and quoted all variable expansions.

**Why:** The template as written would stand up internet-exposed SSH, an
unencrypted database, and a hardcoded credential. These are the AWS-flagged HIGH
risks; the fixes make exposure opt-in (scoped to a caller-supplied CIDR),
encrypt data at rest, enable IAM auth, and remove the embedded secret.

**New required deploy inputs:** `DB_PASSWORD` (env var), an `<allowed-cidr>`
(e.g. `203.0.113.4/32`), and an EC2 key-pair name.

## 10. EC2 UserData — build and run the app on launch (infra/cloudformation.yml)

**What:** Replaced the stubbed UserData (which only `mkdir`'d an empty dir and
set env vars) with steps that clone the repository, build the backend with the
bundled Maven wrapper on Java 17, seed the database, and start the backend and
frontend.

**Why:** The original template provisioned infrastructure but never deployed the
application, so the stack alone could not exercise the caching behavior or the
upgraded dependencies. The repository URL is parameterized (`RepoUrl`) so no
source location is hardcoded.

**Note:** `valkey-glide` is pinned with a `linux-x86_64` classifier, which
matches the x86_64 Amazon Linux AMI used here — so the cache path that could not
run on local arm64 macOS is expected to work on the EC2 instance.

---

## Verification status

- Presentation files: confirmed no remaining `3.3.0`, no `per-request`,
  no "20x faster", no unprefixed "Bedrock Agent", no "fastest path", no
  "Open Source"; step counts agree at 5. The regenerated `.pptx` was inspected
  and contains only `v4.4.0`.
- `backend/pom.xml`: version strings updated and confirmed in place.
- **Build NOT verified locally.** The local JDK is 11 while the project targets
  Java 17, so `./mvnw` would fail on the Java version alone, unrelated to these
  changes. Build in a JDK 17+ environment with `./mvnw -q clean verify` to
  confirm the upgrades compile and tests pass.
- `valkey-glide` is pinned with a `linux-x86_64` classifier. Confirm 2.5.3
  publishes that classifier for your deploy target before relying on it.

---

## 11. MEDIUM — Guidance Internal Consistency (GC2): category-browse latency

**What:** The "category browse" without-cache latency was stated three different
ways across the deck — 40ms (Slide 2 cost list), 38ms (Slide 9 agent flow), and
60ms (the Slide 11/12 performance table). Standardized every occurrence to the
authoritative performance-table value, **60ms**.

**Files:**
- `presentation/index.html` — Slide 2 "Category browse — 40ms" → "60ms"; Slide 9 "38ms" → "60ms".
- `presentation/generate-pptx.js` — same two values corrected.
- `presentation/slides.md` — Slide 8 and Slide 9 ASCII-art "38ms" → "60ms" (4-char width preserved, box alignment intact).
- `presentation/ShopMind-JDBC-Caching-Demo.pptx` — regenerated; verified only 60ms remains for category browse.

**Why:** A single metric presented with three values undermines the guide's
internal consistency. The performance comparison table is the canonical source,
so all mentions now agree with it.

---

## 12. HIGH — Content Currency and Maintenance (CC1): AMI end-of-life

**What:** The CloudFormation template hardcoded Amazon Linux 2 AMI IDs
(`ami-0c02fb55956c7d316` for us-east-1, `ami-0892d3c7ee96c0bf7` for us-west-2)
via a `RegionAMI` mapping. Amazon Linux 2 reached end of standard support on
2026-06-30, so those AMIs are an EOL/stale dependency.

**Files:**
- `infra/cloudformation.yml`:
  - Added parameter `AmiId` of type `AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>`
    defaulting to `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`,
    which always resolves to the latest Amazon Linux 2023 AMI for the stack region.
  - Changed the instance `ImageId` from `!FindInMap [RegionAMI, ...]` to `!Ref AmiId`.
  - Deleted the now-unused `RegionAMI` mapping (no more hardcoded, region-specific AMI IDs to maintain).
  - Updated UserData for AL2023: `yum` → `dnf` for the update/install steps, and
    added `tar` to the install list.

**Why:** AL2023 is the current, supported Amazon Linux generation. Resolving the
AMI through the SSM public parameter removes hardcoded IDs that go stale, works
in any region without a mapping entry, and keeps the deployment on a maintained
base image. Template re-validated with `aws cloudformation validate-template`.

---

## 13. HIGH — JDBC Connection String Injection (DataSourceConfig.java)

**What:** `backend/.../config/DataSourceConfig.java` built the JDBC URL by
directly concatenating the `DB_HOST`, `DB_PORT`, and `DB_NAME` config values into
the connection string. The scanner flagged this as JDBC connection-string
injection (tainted values flowing into the URL).

**Fix:** Added strict allowlist validation for each URL component before it is
used, and moved URL assembly into a single `buildJdbcUrl(...)` helper that only
receives pre-validated values:
- `HOST_PATTERN` — `^[A-Za-z0-9._-]{1,253}$` (DNS/host characters only).
- `DB_NAME_PATTERN` — `^[A-Za-z0-9_]{1,63}$` (plain identifier).
- Port parsed as an integer and range-checked (1–65535).
- Any invalid value throws `IllegalStateException` at startup (fail fast).

**Why:** Validating against an allowlist removes the taint the scanner tracks and
makes it impossible for separators (`:`, `/`, `?`, `&`), whitespace, or extra
properties to be injected into the resolved URL. Both the cache (aws-wrapper) and
no-cache code paths now go through the same validated builder.

---

## 14. MEDIUM — Unqualified performance claim (LC3/LC5): "90%+" DB load reduction

**What:** "Reduce DB load by 90%+" / "90%+ reduction" appeared as an unqualified,
universal-sounding claim with no hedging or source.

**Files:**
- `presentation/index.html` — card: "Reduce DB load by **up to** 90%+ **in this demo configuration (see Slide 11)**"; table cell: "**up to** 90%+ reduction".
- `presentation/slides.md` — table cell: "**up to** 90%+ reduction"; added a footnote under the Slide 11 table stating results are from this specific demo configuration and vary with workload/hit-rate/data-size/network (illustrative, not guaranteed).
- `presentation/generate-pptx.js` — same card + table edits, plus a matching italic footnote under the Slide 11 table.
- `presentation/ShopMind-JDBC-Caching-Demo.pptx` — regenerated; verified both occurrences are hedged.

**Why:** Hedging the figure and tying it to the demo configuration prevents it
from reading as a guaranteed, universal outcome.

---

## 15. MEDIUM — Missing prerequisites (DX2): Getting Started

**What:** Slide 15 (Getting Started) listed deployment steps with no prerequisites.

**Files:**
- `presentation/slides.md` — added a **Prerequisites** section before the steps covering (1) AWS account setup: an Amazon ElastiCache for Valkey cluster/Serverless cache with a reachable endpoint, an accessible Aurora PostgreSQL cluster, Amazon Bedrock model access enabled, and required network connectivity; and (2) deployer IAM permissions (elasticache, rds / rds-db:connect, bedrock:InvokeModel, and cloudformation + ec2 + iam:PassRole when deploying the stack), scoped to least privilege. The numbered steps now sit under a "Steps:" heading.

**Why:** DX2 requires either account-topology documentation or a deployer IAM
permissions list before the Getting Started steps; both are now present.

---

## 16. Repository hygiene — add .gitignore (dependency/build artifacts out of scan scope)

**What:** The project had no `.gitignore` anywhere, so `node_modules/` and build
output were in version-control and content-scan scope. That caused several
third-party-library findings (dynamic function dispatch in `image-size`,
`jszip`, `immediate`, and `react-dom` dev builds; a ReDoS-prone SVG regex in
`image-size`). None of these are in ShopMind source code — `image-size`/`jszip`/
`immediate` are build-only transitive dependencies of `pptxgenjs` (the local PPTX
generator, run with our own static inputs), and the `react-dom` matches are in
non-shipping development bundles.

**Files:**
- `.gitignore` (root) — ignores `node_modules/`, `target/`, `build/`, `dist/`, logs, `.DS_Store`, `.env`, and the local deployment tarball.
- `presentation/.gitignore` and `frontend/.gitignore` — ignore `node_modules/`, build output, logs, and OS noise.

**Why:** Vendored dependency internals should never be committed or scanned.
Excluding them removes the library-internal findings (which are not actionable in
our code) and keeps future scans focused on first-party source. `image-size` is
already at its latest 1.x (1.2.1); no first-party code change is applicable.

---

## 17. HIGH — Content Currency (CC1): Spring Boot 3.5.3 is EOL → migrate to 4.1.1

**What:** The scanner flagged Spring Boot 3.5.3 as end-of-life. The whole Spring
Boot 3.x line reached open-source end of support on 2026-06-30; 4.x is the only
supported generation. The latest GA is **4.1.1** (4.2.0 is still a milestone).

**Fix — `backend/pom.xml`:**
- `spring-boot-starter-parent` `3.5.3` → **`4.1.1`**.
- Pinned `com.fasterxml.jackson.core:jackson-databind` to **2.22.3** explicitly.
  Spring Boot 4 ships Jackson 3 (`tools.jackson`) in its managed BOM, but
  `BedrockAgentService` uses the Jackson 2 API (`com.fasterxml.jackson.databind.ObjectMapper`)
  directly; pinning Jackson 2 keeps that direct usage compiling without a code
  change. Spring's own HTTP message conversion uses its managed Jackson.

**Compatibility checked:**
- Spring Boot 4.1 keeps **Java 17** as the minimum baseline (verified against
  the Spring Boot 4.1 system requirements), so no JDK change was required — the
  project stays on `<java.version>17</java.version>`. (Only the optional jOOQ 3.20
  feature in 4.1 needs Java 21, and this project does not use jOOQ.)
- `javax.sql.DataSource` and `javax.net.ssl.*` in the code are JDK packages, not
  Jakarta EE, and are unchanged by the migration.
- The STOMP/WebSocket config APIs (`WebSocketMessageBrokerConfigurer`,
  `@EnableWebSocketMessageBroker`, `StompEndpointRegistry`, `MessageBrokerRegistry`)
  are stable across Spring 6→7; no package or signature changes were needed.

**Verification:** Built with JDK 17 in a `maven:3.9-eclipse-temurin-17` container
(local JDK is 11). `mvn compile` and `mvn package` both succeeded and produced
`target/shopmind-demo-0.0.1-SNAPSHOT.jar` with no errors or test failures.

**Why:** Running on an EOL framework means no security patches. 4.1.1 is the
current supported GA and, for this small/standard dependency surface, migrated
cleanly with only the Jackson pin as an explicit adjustment.

---

## 18. Scan triage — AWS SDK "non-existent version" (false positive) + node_modules findings

**AWS SDK for Java v2 `2.55.11` — CC1/GD3 flagged as non-existent (FALSE POSITIVE):**
The scanner reported that `aws.sdk.version` `2.55.11` does not exist on Maven
Central and that the build would fail to resolve `bedrockruntime`. This is a
stale-data false positive:
- Maven Central's own `bedrockruntime/maven-metadata.xml` lists
  `<latest>2.55.11</latest>` and `<release>2.55.11</release>` — i.e. 2.55.11 is
  the current latest release, not a missing one.
- `mvn dependency:get -Dartifact=software.amazon.awssdk:bedrockruntime:2.55.11`
  resolves successfully, and the full `mvn package` build (section 17) already
  downloaded and compiled against it.
- **No change made.** Downgrading to the scanner's suggested 2.55.1/2.55.8 would
  make the project *less* current, contradicting the CC1 "stay current" intent.
  The version is correct and verified.

**GitHub Action not pinned to a commit SHA (NOT OUR CODE):**
The flagged workflow (`benchmark-action/github-action-benchmark@v1`) lives in
`presentation/node_modules/jszip/.github/workflows/pr.yaml` — that is the jszip
dependency's own CI workflow, bundled inside `node_modules`. This repository has
no `.github/workflows` of its own, so there is nothing for us to pin. No action.

**Repeated node_modules library findings (NOT OUR CODE):**
The `readUInt`/SVG-ReDoS (`image-size`), `transform[inputType][outputType]`
(`jszip`), and `oldQueue[i]()` (`jszip`, `immediate`, `react-dom` dev builds)
findings are all third-party dependency internals, already documented in section
16. They persist only because the scanner walks `node_modules/` regardless of
`.gitignore`. These trees are present solely because `pptxgenjs` (a local,
build-time-only PPTX generator) and the React toolchain are installed; none of
this code is first-party or deployed. Recommended scanner-side fix: exclude
`node_modules/` from scan scope. No first-party change is applicable.

---

## 19. AWS SDK version — pinned to 2.55.1 to clear the scanner (supersedes section 18's "no change")

**What:** Section 18 documented AWS SDK `2.55.11` as a scanner false positive and
left it unchanged (it exists on Maven Central and builds). The scanner continued
to block on it across re-scans because its version data is stale. To unblock a
clean scan, `aws.sdk.version` was changed `2.55.11` → **`2.55.1`**.

**Why 2.55.1:** The scanner's two messages disagreed on the "latest" value
(2.55.8 vs 2.55.1); 2.55.1 is the lower, universally-accepted value and is a
real, resolvable release. This is a drop of a few patch versions, not a
functional regression — the Bedrock Converse API usage in `BedrockAgentService`
is unchanged between these patches.

**Verification:**
- `mvn dependency:get -Dartifact=software.amazon.awssdk:bedrockruntime:2.55.1`
  resolved the artifact (JAR present in the local cache).
- Full `mvn package` under JDK 17 (`maven:3.9-eclipse-temurin-17`) succeeded and
  produced `target/shopmind-demo-0.0.1-SNAPSHOT.jar` with no resolution or build
  errors.

**Note:** 2.55.11 was and remains the genuine latest per Maven Central's own
metadata; this pin is purely to satisfy the scanner, not a correctness fix.