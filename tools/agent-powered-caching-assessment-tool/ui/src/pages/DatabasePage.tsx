import { useState } from "react";
import {
  Alert, Box, Button, Container, ContentLayout, ExpandableSection, Header, KeyValuePairs, Link,
  Modal, SpaceBetween, Spinner, StatusIndicator,
} from "@cloudscape-design/components";
import { useCapabilities, useCheckDatabase, useDatabase, useDeleteDatabase } from "../api/hooks";
import type { DatabaseView } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { useNotify } from "../components/Notifications";
import {
  formatTime, jobStatus, readinessLabel, readinessStatus, stateLabel, verdictStatus,
} from "../components/status";
import { runGate } from "../databases/connection";
import { HostProbePanel } from "../databases/HostProbePanel";
import { assessmentRoute, follow, href, navigate } from "../router";
import { AssessmentsTable } from "./HistoryPage";
import { Checklist } from "./NewAssessmentPage";
import { RunModal } from "./RunModal";

const ACTIVE = new Set(["queued", "running"]);

/**
 * Forget a saved database. Its assessments stay.
 * @param stay Stay on the current page after deleting (the databases list); otherwise go home.
 */
export function DeleteDatabaseModal({ db, onClose, stay = false }: { db: DatabaseView; onClose: () => void; stay?: boolean }) {
  const del = useDeleteDatabase();
  const notify = useNotify();
  return (
    <Modal
      visible
      onDismiss={onClose}
      header="Delete database"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={del.isPending}
              onClick={() => del.mutate(db.database_id, {
                onSuccess: () => {
                  notify({ type: "success", content: `Deleted ${db.label}.` });
                  onClose();
                  if (!stay) navigate({ name: "home" });
                },
              })}>
              Delete
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>Delete the saved connection <Box variant="strong">{db.label}</Box>? Its {db.assessments ?? 0} assessment{db.assessments === 1 ? "" : "s"} and their results stay, under Assessments.</Box>
        {del.error ? <ErrorAlert error={del.error} header="Delete failed" /> : null}
      </SpaceBetween>
    </Modal>
  );
}

/** The latest result: the newest complete assessment's verdict, and any run in progress. */
function LatestResult({ db }: { db: DatabaseView }) {
  const done = db.latest_complete;
  const latest = db.latest;
  const moving = latest && ACTIVE.has(latest.status);
  return (
    <Container header={<Header variant="h2">Latest result</Header>}>
      <SpaceBetween size="s">
        {done ? (
          <KeyValuePairs columns={3} items={[
            { label: "Workload verdict", value: (
              <StatusIndicator type={verdictStatus(done.workload_verdict)}>{done.workload_verdict ?? "No verdict"}</StatusIndicator>
            ) },
            { label: "Assessment", value: (
              <Link href={href(assessmentRoute(done.job_id, db.database_id))} onFollow={(e) => follow(e)}>{done.label ?? done.job_id}</Link>
            ) },
            { label: "Started", value: formatTime(done.created_at) },
          ]} />
        ) : (
          <Box color="text-body-secondary">No finished assessment yet.</Box>
        )}
        {moving && latest && (
          <Alert type="info" action={<Button href={href(assessmentRoute(latest.job_id, db.database_id))} onFollow={(e) => follow(e)}>Follow it</Button>}>
            <StatusIndicator type={jobStatus(latest.status)}>{stateLabel(latest.status)}</StatusIndicator> An assessment started {formatTime(latest.created_at)} is not finished.
          </Alert>
        )}
        {!moving && latest && latest.job_id !== done?.job_id && (
          <Box color="text-body-secondary">
            The newest assessment, <Link href={href(assessmentRoute(latest.job_id, db.database_id))} onFollow={(e) => follow(e)}>{latest.label ?? latest.job_id}</Link>, {latest.status}.
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
}

/** One saved database: readiness, latest result, connection and its assessments. */
export function DatabasePage({ databaseId }: { databaseId: string }) {
  const caps = useCapabilities();
  const q = useDatabase(databaseId);
  const check = useCheckDatabase();
  const notify = useNotify();
  const [modal, setModal] = useState<"run" | "delete" | "probe" | null>(null);
  const db = q.data;
  const r = db?.readiness;
  const gate = runGate(r);
  const busy = (db?.running ?? 0) > 0;

  const onCheck = () => check.mutate(databaseId, {
    onSuccess: (res) => notify({ type: res.readiness.status === "blocked" ? "warning" : "success",
      content: `Checked: ${readinessLabel(res.readiness.status)}.` }),
  });

  if (q.error) return <ErrorAlert error={q.error} header="The database could not be loaded" onRetry={() => void q.refetch()} />;
  if (!db) return <Box textAlign="center" padding="xl"><Spinner size="large" /></Box>;
  const c = db.connection;

  return (
    <ContentLayout header={
      <Header variant="h1"
        description={`${c.database_name} on ${c.ssh_target}`}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button href={href({ name: "connect", databaseId })} onFollow={(e) => follow(e)}>Edit</Button>
            <Button disabled={busy} onClick={() => setModal("delete")}
              ariaLabel={busy ? "Delete (an assessment is still running)" : "Delete"}>Delete</Button>
            <Button onClick={() => setModal("probe")}>Probe host</Button>
            <Button iconName="refresh" loading={check.isPending} onClick={onCheck}>Check again</Button>
            <Button variant="primary" disabled={!gate.ok || !caps.data} onClick={() => setModal("run")}
              ariaLabel={gate.ok ? "Run assessment" : `Run assessment (${gate.reason ?? ""})`}>Run assessment</Button>
          </SpaceBetween>
        }>
        {db.label}
      </Header>
    }>
      <SpaceBetween size="l">
        {caps.error ? <ErrorAlert error={caps.error} header="Server capabilities could not be loaded" /> : null}
        {!gate.ok && (
          <Alert type={r?.status === "blocked" ? "error" : "info"}
            action={<Button loading={check.isPending} onClick={onCheck}>Check now</Button>}>
            {gate.reason}
          </Alert>
        )}
        <Container header={<Header variant="h2" description="From the last readiness check. A changed connection must be checked again.">Readiness</Header>}>
          <SpaceBetween size="m">
            <KeyValuePairs columns={4} items={[
              { label: "Status", value: <StatusIndicator type={readinessStatus(r?.status)}>{readinessLabel(r?.status)}</StatusIndicator> },
              { label: "Checked", value: formatTime(r?.checked_at) },
              { label: "PostgreSQL", value: r?.database_version ?? "-" },
              { label: "pg_stat_statements", value: r?.pg_stat_statements_available === true ? "Usable"
                : r?.pg_stat_statements_available === false ? "Not usable (degraded mode)" : "-" },
              ...((r?.failed ?? []).length ? [{ label: "Failed checks", value: (r?.failed ?? []).join(", ") }] : []),
              ...((r?.warned ?? []).length ? [{ label: "Warnings", value: (r?.warned ?? []).join(", ") }] : []),
            ]} />
            {check.error ? <ErrorAlert error={check.error} header="The check could not run" onRetry={onCheck} /> : null}
            {check.data && (
              <ExpandableSection headerText="What the last check said" defaultExpanded>
                <Checklist checks={check.data.result.checks} />
              </ExpandableSection>
            )}
          </SpaceBetween>
        </Container>
        <LatestResult db={db} />
        <Container header={<Header variant="h2" description="No password is stored, only where it is kept.">Connection</Header>}>
          <KeyValuePairs columns={3} items={[
            { label: "SSH target", value: `${c.ssh_target}, port ${c.ssh_port ?? 22}` },
            { label: "Key name", value: c.identity_file || "Server default" },
            { label: "Database user", value: c.db_user },
            { label: "Password source", value: c.password_secret_arn ? `Secrets Manager: ${c.password_secret_arn}` : `File on the host: ${c.password_file ?? "-"}` },
            { label: "Database", value: `${c.database_name} at ${c.cluster_endpoint ?? "127.0.0.1"}:${c.port ?? 5432}` },
            { label: "Schemas", value: c.schemas?.length ? c.schemas.join(", ") : "All" },
          ]} />
        </Container>
        <AssessmentsTable databaseId={databaseId} />
      </SpaceBetween>
      {modal === "run" && caps.data && <RunModal caps={caps.data} db={db} onClose={() => setModal(null)} />}
      {modal === "delete" && <DeleteDatabaseModal db={db} onClose={() => setModal(null)} />}
      {modal === "probe" && (
        <Modal visible size="large" onDismiss={() => setModal(null)} header="Probe this database's host"
          footer={<Box float="right"><Button variant="link" onClick={() => setModal(null)}>Close</Button></Box>}>
          <SpaceBetween size="s">
            <Box color="text-body-secondary">
              Checks that the console can still reach {c.ssh_target} with the saved key, and lists the PostgreSQL clusters
              running there. It does not sign in to PostgreSQL; use Check again for that.
            </Box>
            <HostProbePanel variant="embedded"
              fixed={{ ssh_target: c.ssh_target, ssh_port: String(c.ssh_port ?? 22), identity_file: c.identity_file ?? "" }} />
          </SpaceBetween>
        </Modal>
      )}
    </ContentLayout>
  );
}
