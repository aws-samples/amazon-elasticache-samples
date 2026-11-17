import { useEffect, useMemo, useState } from "react";
import {
  Alert, Box, Button, ColumnLayout, Container, ContentLayout, FileUpload, Form, FormField,
  Header, Input, SegmentedControl, SpaceBetween, Spinner, StatusIndicator, Table,
} from "@cloudscape-design/components";
import { usePreflight, useCapabilities, useSubmitCollection, useSubmitLive } from "../api/hooks";
import type { Capabilities, CollectorInput, PreflightCheck, PreflightRequest } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { checkStatus } from "../components/status";
import { parseLocalStart, windowErrors } from "../chat/clientTools";
import { SECRET_ARN } from "../databases/connection";
import { navigate } from "../router";
import { defaultOptions, JobOptionsForm, optionErrors, optionFields, type OptionValues } from "./JobOptionsForm";

interface LiveFields {
  job_id: string;
  cluster_endpoint: string;
  port: string;
  database_name: string;
  schemas: string;
  ssh_target: string;
  ssh_port: string;
  identity_file: string;
  db_user: string;
  password_source: "file" | "secret";
  password_file: string;
  password_secret_arn: string;
  timeout_minutes: string;
  window_minutes: string;
  start_at: string;
}

const EMPTY: LiveFields = {
  job_id: "", cluster_endpoint: "", port: "", database_name: "", schemas: "", ssh_target: "",
  ssh_port: "", identity_file: "", db_user: "", password_source: "file", password_file: "",
  password_secret_arn: "", timeout_minutes: "", window_minutes: "15", start_at: "",
};

const intOrUndef = (s: string) => (s.trim() === "" ? undefined : Number(s));

function liveErrors(caps: Capabilities, f: LiveFields): Record<string, string> {
  const e: Record<string, string> = {};
  const L = caps.limits;
  if (!f.job_id) e.job_id = "Required";
  else if (!new RegExp(L.job_id_pattern).test(f.job_id) || f.job_id.length > L.job_id_max_length)
    e.job_id = `Letters, digits, dot, dash or underscore; at most ${L.job_id_max_length} characters`;
  for (const k of ["cluster_endpoint", "database_name", "ssh_target", "db_user"] as const)
    if (!f[k].trim()) e[k] = "Required";
  if (f.password_source === "file" && !f.password_file.trim()) e.password_file = "Required";
  if (f.password_source === "secret" && !SECRET_ARN.test(f.password_secret_arn.trim())) e.password_secret_arn = "A Secrets Manager secret ARN";
  for (const k of ["port", "ssh_port"] as const) {
    const v = intOrUndef(f[k]);
    if (v !== undefined && (!Number.isInteger(v) || v < 1 || v > 65535)) e[k] = "A port from 1 to 65535";
  }
  const t = intOrUndef(f.timeout_minutes);
  if (t !== undefined && (!Number.isInteger(t) || t < L.collection_timeout_minutes_min || t > L.collection_timeout_minutes_max))
    e.timeout_minutes = `${L.collection_timeout_minutes_min} to ${L.collection_timeout_minutes_max} minutes`;
  const start = parseLocalStart(f.start_at);
  if (start === null) e.start_at = "As YYYY-MM-DD HH:MM, your local time";
  for (const w of windowErrors(intOrUndef(f.window_minutes), start ?? undefined, caps)) {
    if (w.startsWith("window_minutes")) e.window_minutes = `From ${L.window_minutes_min} to ${L.window_minutes_max} minutes`;
    else e.start_at = w.replace(/^start_at: /, "").replace(/^./, (x) => x.toUpperCase());
  }
  return e;
}

function toInput(f: LiveFields): CollectorInput {
  const schemas = f.schemas.split(",").map((s) => s.trim()).filter(Boolean);
  const port = intOrUndef(f.port);
  const sshPort = intOrUndef(f.ssh_port);
  const timeout = intOrUndef(f.timeout_minutes);
  const windowMinutes = intOrUndef(f.window_minutes);
  const start = parseLocalStart(f.start_at) ?? undefined;
  return {
    job_id: f.job_id,
    cluster_endpoint: f.cluster_endpoint.trim(),
    database_name: f.database_name.trim(),
    ...(port !== undefined ? { port } : {}),
    ...(timeout !== undefined ? { timeout_minutes: timeout } : {}),
    live_config: {
      ssh_target: f.ssh_target.trim(),
      db_user: f.db_user.trim(),
      ...(f.password_source === "secret"
        ? { password_secret_arn: f.password_secret_arn.trim() }
        : { password_file: f.password_file.trim() }),
      ...(sshPort !== undefined ? { ssh_port: sshPort } : {}),
      ...(f.identity_file.trim() ? { identity_file: f.identity_file.trim() } : {}),
    },
    collection_options: {
      ...(schemas.length ? { schemas } : {}),
      ...(windowMinutes !== undefined ? { window_minutes: windowMinutes } : {}),
      ...(start ? { start_at: start } : {}),
    },
  };
}

export function Checklist({ checks }: { checks: ReadonlyArray<PreflightCheck> }) {
  return (
    <Table<PreflightCheck>
      variant="embedded"
      items={[...checks]}
      ariaLabels={{ tableLabel: "Preflight checks" }}
      wrapLines
      columnDefinitions={[
        { id: "name", header: "Check", cell: (c) => c.name },
        { id: "status", header: "Result", cell: (c) => <StatusIndicator type={checkStatus(c.status)}>{c.status}</StatusIndicator> },
        { id: "msg", header: "Message", cell: (c) => (
          <SpaceBetween size="xxs">
            <span>{c.message}</span>
            {c.hint && <Box variant="small" color="text-body-secondary">{c.hint}</Box>}
          </SpaceBetween>
        ) },
      ]}
    />
  );
}

function LiveForm({ caps }: { caps: Capabilities }) {
  const [f, setF] = useState<LiveFields>(EMPTY);
  const [opts, setOpts] = useState<OptionValues>(() => defaultOptions(caps));
  const [label, setLabel] = useState("");
  const [touched, setTouched] = useState(false);
  const preflight = usePreflight();
  const submit = useSubmitLive();

  // Any change to the connection makes the last preflight stale.
  const conn = JSON.stringify({ ...f, job_id: "" });
  useEffect(() => { preflight.reset(); }, [conn]); // eslint-disable-line react-hooks/exhaustive-deps

  const errs = { ...liveErrors(caps, f), ...optionErrors(caps, opts, label) };
  const shown = touched ? errs : {};
  const ok = Object.keys(errs).length === 0;
  const passed = preflight.data?.ok === true;

  const field = (k: keyof LiveFields, labelText: string, description?: string, extra?: { type?: "number" | "text"; placeholder?: string }) => (
    <FormField label={labelText} constraintText={description} errorText={shown[k]}>
      <Input value={f[k]} type={extra?.type ?? "text"} placeholder={extra?.placeholder}
        onChange={({ detail }) => setF((x) => ({ ...x, [k]: detail.value }))} />
    </FormField>
  );

  const runPreflight = () => {
    setTouched(true);
    if (!ok) return;
    const { job_id: _ignored, ...rest } = toInput(f);
    void _ignored;
    preflight.mutate(rest as PreflightRequest);
  };
  const runSubmit = () => {
    setTouched(true);
    if (!ok || !passed) return;
    submit.mutate({ body: toInput(f), query: optionFields(opts, label) }, {
      onSuccess: (r) => navigate({ name: "assessment", id: r.job_id }),
    });
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); runSubmit(); }}>
      <Form
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button formAction="none" onClick={runPreflight} loading={preflight.isPending}>Run preflight</Button>
            <Button variant="primary" formAction="submit" loading={submit.isPending} disabled={!passed}
              ariaLabel={passed ? "Start the assessment" : "Start the assessment (run a passing preflight first)"}>
              Start assessment
            </Button>
          </SpaceBetween>
        }
        errorText={submit.error ? <ErrorAlert error={submit.error} header="The assessment was not accepted" /> : undefined}
      >
        <SpaceBetween size="l">
          <Container header={<Header variant="h2" description="The server reaches the database over SSH. Nothing here is stored except a sanitized copy without the SSH target, user or password file.">Database</Header>}>
            <ColumnLayout columns={2}>
              {field("job_id", "Job id", "Names the job and its folder.")}
              {field("database_name", "Database name")}
              {field("cluster_endpoint", "Database host", "As seen from the SSH host.")}
              {field("port", "Database port", "Leave empty for the server default.", { type: "number" })}
              {field("schemas", "Schemas", "Comma-separated. Leave empty for all.")}
              {field("timeout_minutes", "Collection timeout (minutes)", `${caps.limits.collection_timeout_minutes_min} to ${caps.limits.collection_timeout_minutes_max}. Leave empty for the default.`, { type: "number" })}
              {field("window_minutes", "Collection window (minutes)", `How long to measure, ${caps.limits.window_minutes_min} to ${caps.limits.window_minutes_max}. The statistics are read at the start and the end, so pick a period of normal or peak load.`, { type: "number" })}
              {field("start_at", "Start at", `Optional, your local time, up to ${caps.limits.window_start_max_hours} hours ahead. Leave empty to start at once.`, { placeholder: "YYYY-MM-DD HH:MM" })}
            </ColumnLayout>
          </Container>
          <Container header={<Header variant="h2">SSH and login</Header>}>
            <ColumnLayout columns={2}>
              {field("ssh_target", "SSH target", "A host or an alias in the server's SSH config.")}
              {field("ssh_port", "SSH port", "Leave empty for the default.", { type: "number" })}
              {field("identity_file", "Identity file", "Optional. A key file on the server.")}
              {field("db_user", "Database user")}
              <FormField label="Password source" constraintText={caps.features.secrets_manager ? undefined
                : "This server does not read passwords from Secrets Manager (SECRET_ARN_ALLOWED_PREFIXES is not set)."}>
                <SegmentedControl
                  label="Password source"
                  selectedId={f.password_source}
                  onChange={({ detail }) => setF((x) => ({ ...x, password_source: detail.selectedId === "secret" ? "secret" : "file" }))}
                  options={[{ id: "file", text: "Password file on the host" },
                    { id: "secret", text: "Secrets Manager secret", disabled: !caps.features.secrets_manager }]}
                />
              </FormField>
              {f.password_source === "file"
                ? field("password_file", "Password file", "A file on the database host that holds the password. The password itself is never sent.")
                : field("password_secret_arn", "Secret ARN", "The server reads it with its own AWS credentials and sends it to psql over SSH.")}
            </ColumnLayout>
          </Container>
          <Container header={<Header variant="h2">Options</Header>}>
            <JobOptionsForm caps={caps} values={opts} onChange={setOpts} label={label} onLabel={setLabel} errors={shown} />
          </Container>
          <Container header={<Header variant="h2" description="Required before a live assessment is queued.">Preflight</Header>}>
            {preflight.isPending && <Box><Spinner /> Checking the connection</Box>}
            {preflight.error ? <ErrorAlert error={preflight.error} header="Preflight could not run" /> : null}
            {preflight.data && (
              <SpaceBetween size="s">
                <StatusIndicator type={preflight.data.ok ? "success" : "error"}>
                  {preflight.data.ok ? "All checks passed" : "A check failed"}{preflight.data.database_version ? ` · ${preflight.data.database_version}` : ""}
                </StatusIndicator>
                <Checklist checks={preflight.data.checks} />
              </SpaceBetween>
            )}
            {!preflight.data && !preflight.isPending && !preflight.error && (
              <Box color="text-body-secondary">Not run yet for these settings.</Box>
            )}
          </Container>
        </SpaceBetween>
      </Form>
    </form>
  );
}

function CollectionForm({ caps }: { caps: Capabilities }) {
  const [files, setFiles] = useState<File[]>([]);
  const [parsed, setParsed] = useState<Record<string, unknown> | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [opts, setOpts] = useState<OptionValues>(() => defaultOptions(caps));
  const [label, setLabel] = useState("");
  const submit = useSubmitCollection();
  const errs = optionErrors(caps, opts, label);
  const max = caps.limits.max_request_bytes;

  useEffect(() => {
    setParsed(null);
    setFileError(null);
    const file = files[0];
    if (!file) return;
    if (file.size > max) {
      setFileError(`The file is ${file.size.toLocaleString()} bytes; the server accepts at most ${max.toLocaleString()}.`);
      return;
    }
    let live = true;
    file.text().then((t) => {
      if (!live) return;
      try {
        const v: unknown = JSON.parse(t);
        if (typeof v !== "object" || v === null || Array.isArray(v)) setFileError("The file must hold one JSON object: a collection.");
        else setParsed(v as Record<string, unknown>);
      } catch {
        setFileError("The file is not valid JSON.");
      }
    }, () => live && setFileError("The file could not be read."));
    return () => { live = false; };
  }, [files, max]);

  const ready = !!parsed && Object.keys(errs).length === 0;
  return (
    <form onSubmit={(e) => {
      e.preventDefault();
      if (!ready || !parsed) return;
      submit.mutate({ collector_output: parsed, ...optionFields(opts, label) }, {
        onSuccess: (r) => navigate({ name: "assessment", id: r.job_id }),
      });
    }}>
      <Form
        actions={<Button variant="primary" formAction="submit" disabled={!ready} loading={submit.isPending}>Start assessment</Button>}
        errorText={submit.error ? <ErrorAlert error={submit.error} header="The assessment was not accepted" /> : undefined}
      >
        <SpaceBetween size="l">
          <Container header={<Header variant="h2" description="A collection saved by an earlier run or the collect script. No database is contacted.">Saved collection</Header>}>
            <FormField label="Collection file" constraintText={`JSON, at most ${max.toLocaleString()} bytes.`} errorText={fileError ?? undefined}>
              <FileUpload
                value={files}
                onChange={({ detail }) => setFiles(detail.value)}
                accept="application/json,.json"
                showFileSize
                i18nStrings={{
                  uploadButtonText: () => "Choose file",
                  dropzoneText: () => "Drop a collection file to upload",
                  removeFileAriaLabel: (i) => `Remove file ${i + 1}`,
                  limitShowFewer: "Show fewer files",
                  limitShowMore: "Show more files",
                  errorIconAriaLabel: "Error",
                  warningIconAriaLabel: "Warning",
                }}
              />
            </FormField>
          </Container>
          <Container header={<Header variant="h2">Options</Header>}>
            <JobOptionsForm caps={caps} values={opts} onChange={setOpts} label={label} onLabel={setLabel} errors={errs} />
          </Container>
        </SpaceBetween>
      </Form>
    </form>
  );
}

export function NewAssessmentPage() {
  const caps = useCapabilities();
  const [source, setSource] = useState<"live" | "collection">("live");
  const notes = useMemo(() => caps.data?.notes ?? [], [caps.data]);
  return (
    <ContentLayout header={<Header variant="h1" description="Assess a live database over SSH, or a collection saved earlier.">New assessment</Header>}>
      <SpaceBetween size="l">
        {caps.isLoading && <Box textAlign="center"><Spinner size="large" /></Box>}
        {caps.error ? <ErrorAlert error={caps.error} header="Server capabilities could not be loaded" onRetry={() => void caps.refetch()} /> : null}
        {caps.data && (
          <>
            <SegmentedControl
              label="Source"
              selectedId={source}
              onChange={({ detail }) => setSource(detail.selectedId === "collection" ? "collection" : "live")}
              options={[{ id: "live", text: "Live database" }, { id: "collection", text: "Saved collection" }]}
            />
            {source === "live" ? <LiveForm caps={caps.data} /> : <CollectionForm caps={caps.data} />}
            {notes.length > 0 && (
              <Alert type="info" header="About this server">
                <ul style={{ margin: 0, paddingInlineStart: "1.25em" }}>{notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
              </Alert>
            )}
          </>
        )}
      </SpaceBetween>
    </ContentLayout>
  );
}
