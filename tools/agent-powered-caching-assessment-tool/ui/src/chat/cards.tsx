// Tool results as Cloudscape components inside the conversation. Every card redraws from
// the API (through the shared query cache) or shows the API's own response: nothing here
// restates a number the model wrote.

import { useMemo, useState } from "react";
import {
  Alert, Badge, Box, Button, ColumnLayout, Container, ExpandableSection, FileUpload, FormField,
  Header, Input, KeyValuePairs, Link, SegmentedControl, SpaceBetween, Spinner, StatusIndicator, Table,
  Toggle,
} from "@cloudscape-design/components";
import { useQuery } from "@tanstack/react-query";
import { htmlReportUrl } from "../api/client";
import { ApiError } from "../api/errors";
import {
  fetchQueryItem, fetchTableItem, keys, useCapabilities, useJobStatus, useOverview,
} from "../api/hooks";
import type { Capabilities, PreflightCheck, PreflightResult } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { formatTime, jobStatus, stateLabel } from "../components/status";
import { JobMonitor } from "../monitor/JobMonitor";
import { Checklist } from "../pages/NewAssessmentPage";
import { defaultOptions, JobOptionsForm, optionErrors, optionFields, type OptionValues } from "../pages/JobOptionsForm";
import { BulletList, Code, Shown, Verdict } from "../report/common";
import { follow, href } from "../router";
import { modelTurnedOff } from "../components/status";
import { checkCitations, summarise, type CheckedCitation, type CitationIn } from "./citations";
import { useChat } from "./ChatContext";
import { CLIENT_TOOLS, isClientTool, needsTypedConfirm, newJobId, parseLocalStart, windowErrors, type Plan } from "./clientTools";
import { CheckOutcome, DatabaseCard, DatabasesCard, DiscoveryCard } from "./databaseCards";
import type { ToolEntry } from "./conversation";
import { HostProbePanel } from "../databases/HostProbePanel";
import { probeSummary } from "../databases/hostProbe";
import { isRecord, type JsonObject } from "./protocol";

const s = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const reportHref = (job: string) => href({ name: "assessment", id: job });

// --- The trace under every tool ----------------------------------------------------

const WHERE: Record<ToolEntry["executor"], string> = {
  server: "ran on the server", client: "runs in your browser", unknown: "not a known tool",
  local: "ran in your browser (no model)",
};

export function ToolTrace({ e, hideInput }: { e: ToolEntry; hideInput?: boolean }) {
  const st = e.phase === "success" ? "success" : e.phase === "error" ? "error"
    : e.phase === "declined" ? "stopped" : e.phase === "awaiting" ? "pending" : "in-progress";
  return (
    <ExpandableSection variant="footer" headerText={
      <SpaceBetween direction="horizontal" size="xs">
        <Box variant="code" fontSize="body-s">{e.name}</Box>
        <StatusIndicator type={st}>{WHERE[e.executor]}</StatusIndicator>
        {e.requestId && <Box variant="small" color="text-body-secondary">Request ID {e.requestId}</Box>}
      </SpaceBetween>}>
      <SpaceBetween size="xs">
        {!hideInput && <><Box variant="awsui-key-label">Parameters</Box><Code>{JSON.stringify(e.input, null, 2)}</Code></>}
        {e.content && <><Box variant="awsui-key-label">Response</Box><Code>{JSON.stringify(e.content, null, 2)}</Code></>}
      </SpaceBetween>
    </ExpandableSection>
  );
}

function ToolError({ e }: { e: ToolEntry }) {
  const c = e.content ?? {};
  const err = new ApiError(typeof c.http_status === "number" ? c.http_status : 0,
    s(c.code) ?? "error", s(c.detail) ?? "The tool failed.", e.requestId ?? null);
  return <ErrorAlert error={err} header={`${e.name} failed`} />;
}

// --- Read results ------------------------------------------------------------------

/** Whether the AI review ran, and if it was asked for but not used, a warning. */
export function ModelNotice({ meta }: { meta: { interpretation_mode: string; fallback_reason?: string | null } }) {
  if (meta.interpretation_mode === "llm") return null;
  if (modelTurnedOff(meta.fallback_reason)) {
    return <Alert type="info">The AI review was off for this run, so every verdict is the score band.</Alert>;
  }
  return (
    <Alert type="warning" header="The model was requested but not used">
      Every verdict is the score band, not the AI review.{meta.fallback_reason ? ` Reason: ${meta.fallback_reason}` : ""}
    </Alert>
  );
}


function StatusCard({ jobId }: { jobId: string }) {
  const caps = useCapabilities();
  const st = useJobStatus(jobId);
  if (st.error) return <ErrorAlert error={st.error} header={`Could not read ${jobId}`} />;
  if (!st.data || !caps.data) return <Box><Spinner /> Reading {jobId}</Box>;
  const done = st.data.status === "complete";
  return (
    <SpaceBetween size="s">
      <JobMonitor status={st.data} phases={caps.data.phases} />
      {done && (
        <Button href={reportHref(jobId)} onFollow={(ev) => follow(ev)} iconName="angle-right" iconAlign="right">
          View full report
        </Button>
      )}
    </SpaceBetween>
  );
}

function JobsCard({ content }: { content: JsonObject }) {
  const items = Array.isArray(content.items) ? content.items.filter(isRecord) : [];
  return (
    <Table
      variant="embedded"
      header={<Header variant="h3" counter={`(${typeof content.total === "number" ? content.total : items.length})`}>Recent assessments</Header>}
      items={items}
      ariaLabels={{ tableLabel: "Recent assessments" }}
      empty={<Box textAlign="center">No assessments yet.</Box>}
      wrapLines={false}
      columnDefinitions={[
        { id: "id", header: "Assessment", cell: (j) => {
          const id = s(j.job_id) ?? "";
          return <Link href={reportHref(id)} onFollow={(ev) => follow(ev)}>{s(j.label) ?? id}</Link>; // nosemgrep: react-href-var - href() always returns a #/ route with encoded ids
        } },
        { id: "status", header: "Status", cell: (j) => {
          const st = s(j.status) ?? "queued";
          return <StatusIndicator type={jobStatus(st)}>{stateLabel(st)}</StatusIndicator>;
        } },
        { id: "db", header: "Database", cell: (j) => s(j.database_name) ?? "-" },
        { id: "verdict", header: "Workload verdict", cell: (j) => s(j.workload_verdict) ?? "-" },
        { id: "created", header: "Created", cell: (j) => formatTime(s(j.created_at)) },
      ]}
    />
  );
}

/** The report, compact: the same view model as the full report, its own words. */
function ReportCard({ jobId }: { jobId: string }) {
  const ov = useOverview(jobId);
  if (ov.error) return <ErrorAlert error={ov.error} header="The report could not be loaded" />;
  if (!ov.data) return <Box><Spinner /> Loading the report</Box>;
  const o = ov.data;
  const h = o.headline;
  const tone = h.tone === "yes" ? "success" : h.tone === "no" ? "warning" : "info";
  return (
    <Container header={<Header variant="h3" description={o.source.line}
      actions={<Button href={reportHref(jobId)} onFollow={(ev) => follow(ev)} iconName="angle-right" iconAlign="right">View full report</Button>}>
      Report: {jobId}</Header>}>
      <SpaceBetween size="m">
        <Alert type={tone} header={h.title}>
          <SpaceBetween size="xs">
            <Box>{h.finding}</Box>
            {h.bottleneck && <Box><Box variant="span" fontWeight="bold">Bottleneck:</Box> {h.bottleneck}</Box>}
          </SpaceBetween>
        </Alert>
        {o.meta.assessment_mode === "degraded" && (
          <Alert type="warning">Degraded: query-level statistics were not available, so confidence is capped at 60.</Alert>
        )}
        <ModelNotice meta={o.meta} />
        <ColumnLayout columns={Math.min(4, Math.max(1, o.at_a_glance.length))} variant="text-grid">
          {o.at_a_glance.map((g) => (
            <div key={g.id}>
              <Box variant="awsui-key-label">{g.label}</Box>
              <Box variant="awsui-value-large"><Shown value={g.display} /></Box>
              <Box variant="small" color="text-body-secondary">{g.note}</Box>
            </div>
          ))}
        </ColumnLayout>
      </SpaceBetween>
    </Container>
  );
}

function ItemsCard({ e }: { e: ToolEntry }) {
  const c = e.content ?? {};
  const items = Array.isArray(c.items) ? c.items.filter(isRecord) : [];
  const isQuery = e.input.kind === "query";
  return (
    <Table
      variant="embedded"
      header={<Header variant="h3" counter={`(${typeof c.total === "number" ? c.total : items.length})`}>{isQuery ? "Queries" : "Tables"}</Header>}
      items={items}
      ariaLabels={{ tableLabel: isQuery ? "Queries" : "Tables" }}
      wrapLines={false}
      columnDefinitions={[
        ...(isQuery ? [{ id: "rank", header: "#", cell: (i: JsonObject) => String(i.rank ?? "") }] : []),
        { id: "name", header: isQuery ? "Query" : "Table", cell: (i) => isQuery
          ? <Box variant="code" fontSize="body-s">{s(i.sql_short) ?? s(i.queryid)}</Box> : s(i.table_id) },
        { id: "verdict", header: "Verdict", cell: (i) => s(i.verdict_label) ?? (i.not_scored ? "Not cacheable" : "-") },
        { id: "score", header: "Score", cell: (i) => i.hard_excluded
          ? <Box color="text-status-error">Hard Exclusion</Box> : <Shown value={s(i.score)} missing="Not scored" /> },
      ]}
    />
  );
}

function SummaryCard({ content }: { content: JsonObject }) {
  const pairs: Array<[string, unknown]> = [
    ["Verdict", content.verdict], ["Key finding", content.key_finding],
    ["Queries recommended", content.queries_recommended], ["Tables recommended", content.tables_recommended],
    ["Cacheable DB time (ms/s)", content.cacheable_db_time_ms_per_sec], ["Interpretation", content.interpretation_mode],
  ];
  return <KeyValuePairs columns={3} items={pairs.map(([label, v]) => ({ label,
    value: <Shown value={v === null || v === undefined ? null : String(v)} /> }))} />;
}

function SizingCard({ content }: { content: JsonObject }) {
  const sum = isRecord(content.summary) ? content.summary : {};
  const total = sum.cost_available && typeof sum.monthly_total === "number" ? `about $${sum.monthly_total}/month On-Demand (an estimate, not a quote)` : null;
  return (
    <KeyValuePairs columns={3} items={[
      { label: "Status", value: <Shown value={s(sum.status)} /> },
      { label: "Node", value: <Shown value={s(sum.instance_type) ? `${String(sum.total_nodes ?? "?")} × ${s(sum.instance_type)}` : null} missing="None chosen" /> },
      { label: "Cost", value: <Shown value={total} missing="Not priced" /> },
    ]} />
  );
}

function OpenReportCard({ e }: { e: ToolEntry }) {
  const job = s(e.input.job_id) ?? "";
  return e.input.view === "html" ? (
    <Button href={htmlReportUrl(job)} target="_blank" iconName="external" iconAlign="right">
      Open the HTML report for {job}
    </Button>
  ) : (
    <Button href={reportHref(job)} onFollow={(ev) => follow(ev)} iconName="angle-right" iconAlign="right">
      Open the full report for {job}
    </Button>
  );
}

// --- Answers -------------------------------------------------------------------------

const CHECK: Record<CheckedCitation["check"], { type: "success" | "info" | "warning" | "error"; text: string }> = {
  verified: { type: "success", text: "Matches the report" },
  found_elsewhere: { type: "info", text: "In the report, at another field" },
  unverified: { type: "warning", text: "Not found in the report" },
  contradicts: { type: "error", text: "Differs from the report" },
};

function CitationList({ checked }: { checked: CheckedCitation[] }) {
  if (!checked.length) return <Box color="text-status-inactive">No signals were cited.</Box>;
  const { ok, bad } = summarise(checked);
  return (
    <ExpandableSection variant="footer" defaultExpanded headerText={`Signals used (${checked.length})`}
      headerDescription={bad ? `${bad} could not be matched to the report` : `All ${ok} match the report`}>
      <ul style={{ margin: 0, paddingInlineStart: "1.25em" }} aria-label="Signals used">
        {checked.map((c, i) => (
          <li key={i}>
            <SpaceBetween direction="horizontal" size="xs">
              <Box variant="span" fontWeight="bold">{c.label}:</Box>
              <Box variant="span">{c.value}</Box>
              <Box variant="code" fontSize="body-s" color="text-body-secondary">{c.field}</Box>
              <StatusIndicator type={CHECK[c.check].type}>{CHECK[c.check].text}</StatusIndicator>
            </SpaceBetween>
            {(c.actual || c.note) && (
              <Box variant="small" color="text-status-error">
                {c.note ?? ""}{c.actual ? ` The report holds: ${c.actual}` : ""}
              </Box>
            )}
          </li>
        ))}
      </ul>
    </ExpandableSection>
  );
}

function AnswerCard({ e }: { e: ToolEntry }) {
  const job = s(e.input.job_id) ?? "";
  const kind = s(e.input.kind) ?? "workload";
  const itemId = s(e.input.item_id);
  const ov = useOverview(job);
  const qItem = useQuery({
    queryKey: keys.query(job, itemId ?? ""), queryFn: () => fetchQueryItem(job, itemId ?? ""),
    enabled: kind === "query" && !!itemId && !!job, retry: false, staleTime: 60_000,
  });
  const tItem = useQuery({
    queryKey: keys.table(job, itemId ?? ""), queryFn: () => fetchTableItem(job, itemId ?? ""),
    enabled: kind === "table" && !!itemId && !!job, retry: false, staleTime: 60_000,
  });
  const item = kind === "query" ? qItem : tItem;
  const citations: CitationIn[] = Array.isArray(e.input.citations)
    ? e.input.citations.filter(isRecord).map((c) => ({ label: s(c.label) ?? "", value: String(c.value ?? ""), field: s(c.field) ?? "" }))
    : [];
  const source = kind === "workload" ? ov.data : item.data;
  const checked = useMemo(() => (source ? checkCitations(citations, source) : []), [source, citations]);
  const m = item.data?.metrics;
  const meta = ov.data?.meta;
  return (
    <Container header={
      <Header variant="h3"
        actions={job ? <Button href={reportHref(job)} onFollow={(ev) => follow(ev)} iconName="angle-right" iconAlign="right">View full report</Button> : undefined}
        description={kind === "workload" ? `Assessment ${job}` : `${kind === "query" ? "Query" : "Table"} ${itemId ?? ""} in ${job}`}>
        <SpaceBetween direction="horizontal" size="s">
          <span>From the report</span>
          {item.data && <Verdict verdict={item.data.verdict} label={item.data.verdict_label} m={m} byModel={item.data.judged_by_model} />}
          {e.input.grounded === false && <Badge color="grey">Not answerable from this report</Badge>}
        </SpaceBetween>
      </Header>}>
      <SpaceBetween size="s">
        {meta?.assessment_mode === "degraded" && <Alert type="warning">Degraded run: query-level statistics were unavailable and confidence is capped at 60.</Alert>}
        {meta && <ModelNotice meta={meta} />}
        <Box variant="p"><span style={{ whiteSpace: "pre-wrap" }}>{s(e.input.answer) ?? ""}</span></Box>
        {item.error ? <ErrorAlert error={item.error} header="The item could not be read to check the citations" /> : null}
        {source ? <CitationList checked={checked} /> : (kind !== "workload" && !itemId ? null : <Box><Spinner /> Checking citations</Box>)}
      </SpaceBetween>
    </Container>
  );
}

/** The no-model answer: the grounding module's own blocks and citations. */
function LocalAnswerCard({ e }: { e: ToolEntry }) {
  const c = e.content ?? {};
  const blocks = Array.isArray(c.blocks) ? c.blocks.filter(isRecord) : [];
  const cites = Array.isArray(c.citations) ? c.citations.filter(isRecord) : [];
  const job = s(e.input.job_id) ?? "";
  return (
    <Container header={<Header variant="h3" description="Built from the report's fields. No model was used."
      actions={job ? <Button href={reportHref(job)} onFollow={(ev) => follow(ev)}>View full report</Button> : undefined}>
      <SpaceBetween direction="horizontal" size="s"><span>From the report</span>
        {c.grounded === false && <Badge color="grey">Not answerable from this report</Badge>}</SpaceBetween></Header>}>
      <SpaceBetween size="xs">
        {blocks.map((b, i) => b.kind === "paragraph" ? <Box key={i} variant="p">{s(b.text)}</Box>
          : b.kind === "notice" ? <Alert key={i} type={b.tone === "warning" ? "warning" : "info"}>{s(b.text)}</Alert>
            : <div key={i}><Box variant="h5">{s(b.title)}</Box><BulletList items={Array.isArray(b.items) ? b.items.map(String) : []} label={s(b.title) ?? "List"} /></div>)}
        {cites.length > 0 && (
          <ExpandableSection variant="footer" headerText={`Signals used (${cites.length})`}>
            <ul style={{ margin: 0, paddingInlineStart: "1.25em" }}>
              {cites.map((x, i) => <li key={i}><b>{s(x.label)}:</b> {s(x.value)} <Box variant="code" fontSize="body-s">{s(x.source)}</Box></li>)}
            </ul>
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Container>
  );
}

// --- Forms -------------------------------------------------------------------------

type ConnFields = Record<"job_id" | "cluster_endpoint" | "port" | "database_name" | "schemas" | "ssh_target" | "ssh_port" | "identity_file" | "db_user" | "password_file" | "password_secret_arn" | "window_minutes" | "start_at", string>;

const ARN = /^arn:aws[a-z-]*:secretsmanager:[a-z]{2}(-gov)?-[a-z]+-\d:\d{12}:secret:[A-Za-z0-9/_+=.@-]{1,512}$/;

function ConnectionForm({ e, caps }: { e: ToolEntry; caps: Capabilities }) {
  const chat = useChat();
  const secretsOn = caps.features.secrets_manager === true;
  const [source, setSource] = useState<"file" | "secret">("file");
  const [f, setF] = useState<ConnFields>({
    job_id: newJobId(), cluster_endpoint: "127.0.0.1", port: "", database_name: "", schemas: "",
    ssh_target: "", ssh_port: "", identity_file: "", db_user: "", password_file: "", password_secret_arn: "",
    window_minutes: "15", start_at: "",
  });
  const [touched, setTouched] = useState(false);
  const L = caps.limits;
  const errs: Partial<Record<keyof ConnFields, string>> = {};
  if (!new RegExp(L.job_id_pattern).test(f.job_id) || f.job_id.length > L.job_id_max_length) errs.job_id = "Letters, digits, dot, dash or underscore";
  for (const k of ["cluster_endpoint", "database_name", "ssh_target", "db_user"] as const) if (!f[k].trim()) errs[k] = "Required";
  if (source === "file" && !f.password_file.trim()) errs.password_file = "Required";
  if (source === "secret" && !ARN.test(f.password_secret_arn.trim())) errs.password_secret_arn = "A Secrets Manager secret ARN";
  for (const k of ["port", "ssh_port"] as const) {
    if (f[k] && !(Number.isInteger(Number(f[k])) && Number(f[k]) >= 1 && Number(f[k]) <= 65535)) errs[k] = "A port from 1 to 65535";
  }
  const startAt = parseLocalStart(f.start_at);
  const minutes = f.window_minutes.trim() === "" ? undefined : Number(f.window_minutes);
  if (startAt === null) errs.start_at = "As YYYY-MM-DD HH:MM, your local time";
  for (const w of windowErrors(minutes, startAt ?? undefined, caps)) {
    if (w.startsWith("window_minutes")) errs.window_minutes = `From ${L.window_minutes_min} to ${L.window_minutes_max} minutes`;
    else errs.start_at = w.replace(/^start_at: /, "").replace(/^./, (x) => x.toUpperCase());
  }
  const shown = touched ? errs : {};
  // Hints go under the input (constraintText), so every input in a row lines up.
  const field = (k: keyof ConnFields, label: string, hint?: string, placeholder?: string) => (
    <FormField label={label} constraintText={hint} errorText={shown[k]}>
      <Input value={f[k]} placeholder={placeholder} onChange={({ detail }) => setF((x) => ({ ...x, [k]: detail.value }))} />
    </FormField>
  );
  if (e.phase !== "awaiting") {
    return <StatusIndicator type={e.phase === "success" ? "success" : "stopped"}>
      {e.phase === "success" ? `Connection details given for ${s(e.content?.database_name) ?? "the database"}. They stay in this browser.` : "No connection given."}
    </StatusIndicator>;
  }
  return (
    <Container header={<Header variant="h3" description="These stay in this browser and go only to the server. The assistant never sees them.">Database connection</Header>}>
      <form onSubmit={(ev) => {
        ev.preventDefault();
        setTouched(true);
        if (Object.keys(errs).length) return;
        const schemas = f.schemas.split(",").map((x) => x.trim()).filter(Boolean);
        chat.submitConnection(e, {
          job_id: f.job_id, cluster_endpoint: f.cluster_endpoint.trim(), database_name: f.database_name.trim(),
          ssh_target: f.ssh_target.trim(), db_user: f.db_user.trim(),
          ...(source === "secret" ? { password_secret_arn: f.password_secret_arn.trim() } : { password_file: f.password_file.trim() }),
          ...(f.port ? { port: Number(f.port) } : {}), ...(f.ssh_port ? { ssh_port: Number(f.ssh_port) } : {}),
          ...(f.identity_file.trim() ? { identity_file: f.identity_file.trim() } : {}), ...(schemas.length ? { schemas } : {}),
          window_minutes: minutes, ...(startAt ? { start_at: startAt } : {}),
        });
      }}>
        <SpaceBetween size="l">
          <ColumnLayout columns={2}>
            {field("database_name", "Database name")}
            {field("job_id", "Assessment id")}
            {field("cluster_endpoint", "Database host", "As seen from the SSH host")}
            {field("port", "Database port", "Empty for 5432", "5432")}
            {field("ssh_target", "SSH target", "A host or an alias in the server's SSH config")}
            {field("ssh_port", "SSH port", "Empty for 22", "22")}
            {field("db_user", "Database user")}
            {field("schemas", "Schemas", "Comma-separated. Empty for all.")}
            {field("identity_file", "Identity file", "Optional. A key name in the server's key folder")}
          </ColumnLayout>
          <ColumnLayout columns={2}>
            {field("window_minutes", "Collection window (minutes)",
              `How long to measure, ${L.window_minutes_min} to ${L.window_minutes_max}. Pick a period of normal or peak load.`)}
            {field("start_at", "Start at", `Optional, your local time, up to ${L.window_start_max_hours} hours ahead. Empty starts at once.`, "YYYY-MM-DD HH:MM")}
          </ColumnLayout>
          <FormField label="Database password" constraintText={secretsOn ? undefined
            : "This server does not read passwords from Secrets Manager (SECRET_ARN_ALLOWED_PREFIXES is not set)."}>
            <SegmentedControl
              label="Password source"
              selectedId={source}
              onChange={({ detail }) => setSource(detail.selectedId === "secret" ? "secret" : "file")}
              options={[{ id: "file", text: "Password file on the host" }, { id: "secret", text: "Secrets Manager secret", disabled: !secretsOn }]}
            />
          </FormField>
          {source === "file"
            ? field("password_file", "Password file", "A file on the database host. The password itself is never sent.")
            : field("password_secret_arn", "Secret ARN", "The server reads it with its own AWS credentials and sends it to psql over SSH.",
              "arn:aws:secretsmanager:<region>:<account>:secret:<name>")}
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="primary" formAction="submit">Use this connection</Button>
            <Button formAction="none" onClick={() => chat.decline(e)}>Cancel</Button>
          </SpaceBetween>
        </SpaceBetween>
      </form>
    </Container>
  );
}

function CollectionForm({ e, caps }: { e: ToolEntry; caps: Capabilities }) {
  const chat = useChat();
  const [files, setFiles] = useState<File[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const max = caps.limits.max_request_bytes;
  if (e.phase !== "awaiting") {
    return <StatusIndicator type={e.phase === "success" ? "success" : "stopped"}>{e.phase === "success" ? "Collection file given. It stays in this browser." : "No file given."}</StatusIndicator>;
  }
  const use = async () => {
    const file = files[0];
    if (!file) return;
    if (file.size > max) { setErr(`At most ${max.toLocaleString()} bytes`); return; }
    try {
      const v: unknown = JSON.parse(await file.text());
      if (!isRecord(v)) { setErr("The file must hold one JSON object"); return; }
      chat.submitCollection(e, { name: file.name, size: file.size, data: v });
    } catch {
      setErr("The file is not valid JSON");
    }
  };
  return (
    <Container header={<Header variant="h3" description="The file stays in this browser until you confirm the assessment.">Saved collection</Header>}>
      <SpaceBetween size="s">
        <FormField label="Collection file" errorText={err ?? undefined} constraintText={`JSON, at most ${max.toLocaleString()} bytes`}>
          <FileUpload value={files} onChange={({ detail }) => { setFiles(detail.value); setErr(null); }} accept="application/json,.json" showFileSize
            i18nStrings={{ uploadButtonText: () => "Choose file", dropzoneText: () => "Drop a collection file", removeFileAriaLabel: (i) => `Remove file ${i + 1}`,
              limitShowFewer: "Show fewer", limitShowMore: "Show more", errorIconAriaLabel: "Error", warningIconAriaLabel: "Warning" }} />
        </FormField>
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" disabled={!files.length} onClick={() => void use()}>Use this file</Button>
          <Button onClick={() => chat.decline(e)}>Cancel</Button>
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
}

/**
 * request_host_probe: the user types the host and probes it here. The model gets only the
 * summary (statuses and fixed text), never the host, user, key or this machine's IP.
 */
function HostProbeForm({ e }: { e: ToolEntry }) {
  const chat = useChat();
  if (e.phase !== "awaiting") {
    const c = e.content ?? {};
    if (e.phase !== "success") return <StatusIndicator type="stopped">No host probed.</StatusIndicator>;
    return <StatusIndicator type={c.ok ? "success" : "warning"}>
      {c.ok ? "The host was probed." : `The probe stopped at: ${s(c.failed_stage) ?? "an early stage"}.`} The host stays in this browser.
    </StatusIndicator>;
  }
  return (
    <Container header={<Header variant="h3" description="The host, user and key stay in this browser. The assistant gets only which stages passed.">Look on a database host</Header>}>
      <HostProbePanel variant="embedded" onCancel={() => chat.decline(e)}
        onResult={(r) => {
          // An unknown host key waits for the user to trust it (which probes again) or cancel.
          if (r.host_key?.state !== "unknown") chat.submitProbe(e, probeSummary(r) as JsonObject);
        }} />
    </Container>
  );
}

// --- Confirmations and their outcome ---------------------------------------------------

function Outcome({ e }: { e: ToolEntry }) {
  const c = e.content ?? {};
  if (e.phase === "declined") return <StatusIndicator type="stopped">Not run: you did not confirm it.</StatusIndicator>;
  if (e.phase === "error") return <ToolError e={e} />;
  const rid = e.requestId ? <Box variant="small" color="text-body-secondary">Request ID {e.requestId}</Box> : null;
  switch (e.name) {
    case "run_preflight": {
      const r = c as unknown as PreflightResult;
      const checks = Array.isArray(r.checks) ? (r.checks as PreflightCheck[]) : [];
      return (
        <SpaceBetween size="xs">
          <StatusIndicator type={r.ok ? "success" : "error"}>{r.ok ? "All checks passed" : "A check failed"}{r.database_version ? ` · ${r.database_version}` : ""}</StatusIndicator>
          <Checklist checks={checks} />{rid}
        </SpaceBetween>
      );
    }
    case "check_database":
      return <SpaceBetween size="xs"><CheckOutcome content={c} databaseId={s(e.input.database_id)} />{rid}</SpaceBetween>;
    case "start_assessment":
    case "rerun_assessment":
    case "run_database_assessment": {
      const job = s(c.job_id);
      return <SpaceBetween size="s"><StatusIndicator type="success">Submitted {job}: {s(c.status) ?? "queued"}</StatusIndicator>{rid}{job && <StatusCard jobId={job} />}</SpaceBetween>;
    }
    case "cancel_assessment":
      return <SpaceBetween size="xxs"><StatusIndicator type="success">{s(c.message) ?? "Cancel requested."}</StatusIndicator>{rid}</SpaceBetween>;
    case "delete_assessment":
      return <SpaceBetween size="xxs"><StatusIndicator type="success">Deleted {s(c.deleted)}.</StatusIndicator>{rid}</SpaceBetween>;
    case "whatif_sizing": {
      const sc = isRecord(c.scenario_summary) ? c.scenario_summary : {};
      const base = isRecord(c.baseline) ? c.baseline : {};
      const node = (x: JsonObject) => (s(x.instance_type) ? `${String(x.total_nodes ?? "?")} × ${s(x.instance_type)}` : null);
      const cost = (x: JsonObject) => (x.cost_available && typeof x.monthly_total === "number" ? `$${x.monthly_total}/month` : null);
      return (
        <SpaceBetween size="xs">
          <KeyValuePairs columns={4} items={[
            { label: "Scenario node", value: <Shown value={node(sc)} missing="None" /> },
            { label: "Scenario cost (estimate)", value: <Shown value={cost(sc)} missing="Not priced" /> },
            { label: "Job's node", value: <Shown value={node(base)} missing="None" /> },
            { label: "Job's cost (estimate)", value: <Shown value={cost(base)} missing="Not priced" /> },
          ]} />
          <Box variant="small" color="text-body-secondary">Nothing was saved; the assessment is unchanged. Not a capacity plan and not a quote.</Box>
          {rid}
        </SpaceBetween>
      );
    }
    default:
      return <StatusIndicator type="success">Done.</StatusIndicator>;
  }
}

const SIZING_EDIT = new Set(["whatif_sizing"]);
const OPTIONS_EDIT = new Set(["start_assessment", "rerun_assessment", "run_database_assessment"]);
const WINDOW_EDIT = new Set(["run_database_assessment"]);

/**
 * A saved database run's window, start and degraded mode, editable before confirming.
 * The plan checks the values (planAction), so its errors show under the card.
 * @param props.e The tool entry.
 * @param props.caps GET /capabilities, for the window limits.
 * @param props.onChange Gets the window overrides; a null value removes the key.
 */
function WindowEditor({ e, caps, onChange }: { e: ToolEntry; caps: Capabilities; onChange: (o: JsonObject) => void }) {
  const L = caps.limits;
  const [minutes, setMinutes] = useState(typeof e.input.window_minutes === "number" ? String(e.input.window_minutes) : "");
  const [start, setStart] = useState(s(e.input.start_at) ?? "");
  const [allow, setAllow] = useState(e.input.allow_degraded === true);
  const push = (m: string, st: string, a: boolean) => {
    setMinutes(m); setStart(st); setAllow(a);
    onChange({ window_minutes: m.trim() === "" ? null : Number(m), start_at: st.trim() || null, allow_degraded: a ? true : null });
  };
  return (
    <ExpandableSection headerText="Collection window" defaultExpanded variant="footer"
      headerDescription="Pick a period of normal or peak load.">
      <SpaceBetween size="s">
        <ColumnLayout columns={2}>
          <FormField label="Minutes" constraintText={`${L.window_minutes_min} to ${L.window_minutes_max}`}>
            <Input type="number" inputMode="numeric" value={minutes} onChange={({ detail }) => push(detail.value, start, allow)} />
          </FormField>
          <FormField label="Start at" constraintText={`Optional, your local time, up to ${L.window_start_max_hours} hours ahead. Empty starts at once.`}>
            <Input value={start} placeholder="YYYY-MM-DD HH:MM" onChange={({ detail }) => push(minutes, detail.value, allow)} />
          </FormField>
        </ColumnLayout>
        <Toggle checked={allow} onChange={({ detail }) => push(minutes, start, detail.checked)}
          description="Only for a degraded database: it runs without pg_stat_statements and confidence is capped at 60.">
          Allow degraded mode
        </Toggle>
      </SpaceBetween>
    </ExpandableSection>
  );
}

/** The start or rerun options, editable before confirming. */
function OptionsEditor({ e, caps, onChange }: { e: ToolEntry; caps: Capabilities; onChange: (o: JsonObject) => void }) {
  const rerun = e.name === "rerun_assessment";
  const [values, setValues] = useState<OptionValues>(() => defaultOptions(caps, e.input));
  const [label, setLabel] = useState(s(e.input.label) ?? "");
  const errs = optionErrors(caps, values, label);
  const push = (v: OptionValues, l: string) => {
    setValues(v); setLabel(l);
    onChange({ ...optionFields(v, l), ...(v.use_llm ? {} : { model_id: null, require_llm: null }), ...(l.trim() ? {} : { label: null }) });
  };
  return (
    <ExpandableSection headerText="Options" defaultExpanded variant="footer"
      headerDescription={rerun ? "Leave as they are to rerun with the original's settings." : "Change anything before you confirm."}>
      <JobOptionsForm caps={caps} values={values} label={label} errors={errs} columns={3}
        onChange={(v) => push(v, label)} onLabel={(l) => push(values, l)} />
    </ExpandableSection>
  );
}

/** What-if sizing settings, editable before confirming. */
function SizingEditor({ e, caps, onChange }: { e: ToolEntry; caps: Capabilities; onChange: (o: JsonObject) => void }) {
  const given = isRecord(e.input.parameters) ? e.input.parameters : {};
  const [vals, setVals] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(given).map(([k, v]) => [k, String(v)])));
  const [price, setPrice] = useState<boolean>(e.input.price === true);
  const push = (v: Record<string, string>, p: boolean) => {
    setVals(v); setPrice(p);
    const parameters = Object.fromEntries(Object.entries(v).filter(([, x]) => x !== "").map(([k, x]) => [k, Number(x)]));
    onChange({ parameters, price: p });
  };
  return (
    <ExpandableSection headerText="Sizing settings" defaultExpanded variant="footer" headerDescription="Empty keeps the job's own value.">
      <SpaceBetween size="s">
        <ColumnLayout columns={3}>
          {caps.sizing_parameters.map((p) => (
            <FormField key={p.name} label={p.name} constraintText={`${p.minimum} to ${p.maximum}. Default ${p.default}.`}>
              <Input type="number" inputMode="decimal" value={vals[p.name] ?? ""} placeholder={String(p.default)}
                onChange={({ detail }) => push({ ...vals, [p.name]: detail.value }, price)} />
            </FormField>
          ))}
        </ColumnLayout>
        <Toggle checked={price} onChange={({ detail }) => push(vals, detail.checked)}>Price it with the AWS Pricing API</Toggle>
      </SpaceBetween>
    </ExpandableSection>
  );
}

function ConfirmCard({ e, caps }: { e: ToolEntry; caps: Capabilities }) {
  const chat = useChat();
  const [overrides, setOverrides] = useState<JsonObject>({});
  // The window editor owns its own keys, so it never overwrites the options editor's.
  const [windowOverrides, setWindowOverrides] = useState<JsonObject>({});
  // A key set to null removes it, so "back to the original" really sends nothing for it.
  const effective = useMemo(() => {
    const merged: JsonObject = { ...e.input, ...overrides, ...windowOverrides };
    for (const [k, v] of Object.entries(merged)) if (v === null) delete merged[k];
    return merged;
  }, [e.input, overrides, windowOverrides]);
  const plan: Plan = useMemo(() => chat.plan({ ...e, input: effective }, caps), [chat, e, effective, caps]);
  const [typed, setTyped] = useState("");
  const title = isClientTool(e.name) ? CLIENT_TOOLS[e.name].title : e.name;
  const typedOk = !needsTypedConfirm(e.name) || (plan.jobId !== null && typed === plan.jobId);
  if (e.phase !== "awaiting" && e.phase !== "executing") {
    return <Container header={<Header variant="h3">{title}</Header>}><Outcome e={e} /></Container>;
  }
  const editing = e.phase === "awaiting";
  return (
    <Container header={<Header variant="h3" description="Nothing has run yet. Check it, change it if you need to, then confirm.">Confirm: {title}</Header>}>
      <SpaceBetween size="m">
        {plan.databaseId && !plan.errors.some((x) => x.startsWith("database_id")) && <DatabaseCard databaseId={plan.databaseId} compact />}
        {plan.shown.length > 0 && <KeyValuePairs columns={3} items={plan.shown.map((x) => ({ label: x.label, value: x.value }))} />}
        {editing && WINDOW_EDIT.has(e.name) && <WindowEditor e={e} caps={caps} onChange={setWindowOverrides} />}
        {editing && OPTIONS_EDIT.has(e.name) && <OptionsEditor e={e} caps={caps} onChange={setOverrides} />}
        {editing && SIZING_EDIT.has(e.name) && <SizingEditor e={e} caps={caps} onChange={setOverrides} />}
        {plan.errors.length > 0 && (
          <Alert type="error" header="This cannot be sent yet">
            <BulletList items={plan.errors} label="Problems" />
          </Alert>
        )}
        {needsTypedConfirm(e.name) && plan.jobId && !plan.errors.length && (
          <FormField label={`Type ${plan.jobId} to confirm`} description="Deletes the assessment and everything it holds. It cannot be undone.">
            <Input value={typed} onChange={({ detail }) => setTyped(detail.value)} ariaLabel="Assessment id to confirm deletion" />
          </FormField>
        )}
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" loading={e.phase === "executing"} disabled={!plan.call || !typedOk}
            onClick={() => void chat.confirm(e, plan, sentValues(effective))}>Confirm</Button>
          <Button disabled={e.phase === "executing"} onClick={() => chat.decline(e)}>Decline</Button>
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
}

/** What the model is told was sent. Ids only: never connection details. */
function sentValues(input: JsonObject): JsonObject {
  const out: JsonObject = {};
  for (const [k, v] of Object.entries(input)) if (v !== undefined && v !== null && v !== "") out[k] = v;
  return out;
}

/** A report tool answered "not ready": show where the job is instead. */
function NotReady({ e }: { e: ToolEntry }) {
  const job = s(e.content?.job_id) ?? s(e.input.job_id);
  return (
    <SpaceBetween size="s">
      <StatusIndicator type="pending">{s(e.content?.detail) ?? "Not ready yet."}</StatusIndicator>
      {job && <StatusCard jobId={job} />}
    </SpaceBetween>
  );
}

// --- Dispatcher ----------------------------------------------------------------------

/** The card for one tool, or null for tools that only feed the model (shown as a trace). */
export function ToolCard({ e }: { e: ToolEntry }) {
  const caps = useCapabilities();
  if (e.executor === "unknown") return <Alert type="warning">The assistant asked for a tool that does not exist ({e.name}). Nothing ran.</Alert>;
  if (isClientTool(e.name)) {
    if (!caps.data) return caps.error ? <ErrorAlert error={caps.error} /> : <Spinner />;
    if (CLIENT_TOOLS[e.name].kind === "form") {
      if (e.name === "request_connection_details") return <ConnectionForm e={e} caps={caps.data} />;
      if (e.name === "request_host_probe") return <HostProbeForm e={e} />;
      return <CollectionForm e={e} caps={caps.data} />;
    }
    return <ConfirmCard e={e} caps={caps.data} />;
  }
  if (e.phase === "running") return <Box><Spinner /> Working</Box>;
  if (e.phase === "error") return <ToolError e={e} />;
  if (e.content?.ready === false) return <NotReady e={e} />;
  const job = s(e.input.job_id);
  switch (e.name) {
    case "get_status": return job ? <StatusCard jobId={job} /> : null;
    case "list_assessments": return <JobsCard content={e.content ?? {}} />;
    case "get_report": return job ? <ReportCard jobId={job} /> : null;
    case "get_summary": return <SummaryCard content={e.content ?? {}} />;
    case "list_report_items": return <ItemsCard e={e} />;
    case "get_sizing": return <SizingCard content={e.content ?? {}} />;
    case "present_answer": return <AnswerCard e={e} />;
    case "open_report": return <OpenReportCard e={e} />;
    case "local_answer": return <LocalAnswerCard e={e} />;
    case "list_databases": return <DatabasesCard />;
    case "get_database": {
      const db = s(e.input.database_id);
      return db ? <DatabaseCard databaseId={db} /> : null;
    }
    case "discover_databases": return <DiscoveryCard />;
    default: return null; // ask_report, get_capabilities: inputs to the model only
  }
}

/** Plain text for a screen reader announcing a tool entry. */
export function toolLabel(e: ToolEntry): string {
  return `Tool ${e.name}, ${e.phase}`;
}

