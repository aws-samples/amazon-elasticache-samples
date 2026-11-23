// The assistant's saved database and discovery cards. The model gets these tools'
// results without connection details, so every card redraws from the API through the
// shared query cache: the user sees the labels, hosts and finds, the model does not.

import {
  Box, Button, Header, KeyValuePairs, Link, SpaceBetween, Spinner, StatusIndicator, Table,
} from "@cloudscape-design/components";
import { useDatabase, useDatabases, useDiscovery } from "../api/hooks";
import type { DatabaseCheckResponse, DatabaseView, DiscoveredServer, PreflightCheck } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { formatTime, readinessLabel, readinessStatus } from "../components/status";
import { sourceLabel } from "../databases/connection";
import { Checklist } from "../pages/NewAssessmentPage";
import { follow, href } from "../router";
import { isRecord, type JsonObject } from "./protocol";

const dbHref = (id: string) => href({ name: "database", id });

/**
 * A saved database's readiness as a status line.
 * @param props.db The database.
 */
function ReadinessLine({ db }: { db: DatabaseView }) {
  const r = db.readiness;
  return (
    <StatusIndicator type={readinessStatus(r?.status)}>
      {readinessLabel(r?.status)}{r?.checked_at ? `, checked ${formatTime(r.checked_at)}` : ""}
    </StatusIndicator>
  );
}

/** The list_databases card: the saved databases as GET /databases answers now. */
export function DatabasesCard() {
  const q = useDatabases();
  if (q.error) return <ErrorAlert error={q.error} header="The saved databases could not be read" />;
  if (!q.data) return <Box><Spinner /> Reading the saved databases</Box>;
  return (
    <Table<DatabaseView>
      variant="embedded"
      header={<Header variant="h3" counter={`(${q.data.total})`}>Saved databases</Header>}
      items={q.data.items}
      trackBy="database_id"
      ariaLabels={{ tableLabel: "Saved databases" }}
      empty={
        <SpaceBetween size="xs">
          <Box textAlign="center">No database is saved yet.</Box>
          <Box textAlign="center"><Button href={href({ name: "start" })} onFollow={(e) => follow(e)}>Get started</Button></Box>
        </SpaceBetween>
      }
      wrapLines={false}
      columnDefinitions={[
        { id: "label", header: "Database", cell: (d) => <Link href={dbHref(d.database_id)} onFollow={(e) => follow(e)}>{d.label}</Link> }, // nosemgrep: react-href-var - href() always returns a #/ route with encoded ids
        { id: "readiness", header: "Readiness", cell: (d) => <ReadinessLine db={d} /> },
        { id: "verdict", header: "Latest verdict", cell: (d) => d.latest_complete?.workload_verdict ?? "-" },
        { id: "count", header: "Assessments", cell: (d) => String(d.assessments ?? 0) },
      ]}
    />
  );
}

/**
 * The get_database card, and the header of a database confirm card.
 * @param props.databaseId The saved database.
 * @param props.compact Only the label and the readiness.
 */
export function DatabaseCard({ databaseId, compact = false }: { databaseId: string; compact?: boolean }) {
  const q = useDatabase(databaseId);
  if (q.error) return <ErrorAlert error={q.error} header={`Could not read ${databaseId}`} />;
  if (!q.data) return <Box><Spinner /> Reading {databaseId}</Box>;
  const d = q.data;
  const r = d.readiness;
  const items = [
    { label: "Database", value: <Link href={dbHref(d.database_id)} onFollow={(e) => follow(e)}>{d.label}</Link> },
    { label: "Readiness", value: <ReadinessLine db={d} /> },
    ...(compact ? [] : [
      { label: "PostgreSQL", value: r?.database_version ?? "-" },
      { label: "Failed checks", value: r?.failed?.length ? r.failed.join(", ") : "None" },
      { label: "Warned checks", value: r?.warned?.length ? r.warned.join(", ") : "None" },
      { label: "Assessments", value: `${d.assessments ?? 0}${d.running ? `, ${d.running} running` : ""}` },
      { label: "Latest verdict", value: d.latest_complete?.workload_verdict ?? "-" },
    ]),
  ];
  return <KeyValuePairs columns={compact ? 2 : 3} items={items} />;
}

/**
 * The discover_databases card: the local finds, each with Connect, and a way to look on
 * another host through the connect form (the model cannot name a host).
 */
export function DiscoveryCard() {
  const q = useDiscovery();
  if (q.error) return <ErrorAlert error={q.error} header="Discovery could not run" onRetry={() => void q.refetch()} />;
  if (!q.data) return <Box><Spinner /> Looking for PostgreSQL databases</Box>;
  const servers = q.data.servers ?? [];
  return (
    <Table<DiscoveredServer>
      variant="embedded"
      header={
        <Header variant="h3" counter={`(${servers.length})`}
          description="On this machine. To look on a database host over SSH, type the host in the connect form."
          actions={<Button href={href({ name: "connect" })} onFollow={(e) => follow(e)}>Look on another host</Button>}>
          Databases
        </Header>
      }
      items={servers}
      trackBy="id"
      ariaLabels={{ tableLabel: "Databases" }}
      empty={<Box textAlign="center">No databases found on this machine. Enter the connection details yourself, or look on the database host.</Box>}
      wrapLines
      columnDefinitions={[
        { id: "label", header: "Database or instance", cell: (s) => s.label },
        { id: "source", header: "Source", cell: (s) => sourceLabel(s.source) },
        { id: "version", header: "Version", cell: (s) => s.version ?? "-" },
        { id: "connect", header: "", cell: (s) => (
          <Button variant="inline-link" href={href({ name: "connect", pick: s.id })} onFollow={(e) => follow(e)}>Connect</Button>
        ) },
      ]}
    />
  );
}

/**
 * What check_database answered: the readiness it stored and each check.
 * @param props.content The API's answer (DatabaseCheckResponse) as the tool result holds it.
 * @param props.databaseId The database it checked, for the link.
 */
export function CheckOutcome({ content, databaseId }: { content: JsonObject; databaseId: string | null }) {
  const res = content as unknown as Partial<DatabaseCheckResponse>;
  const status = isRecord(res.readiness) ? res.readiness.status : undefined;
  const result = isRecord(res.result) ? res.result : null;
  const checks = Array.isArray(result?.checks) ? (result.checks as PreflightCheck[]) : [];
  return (
    <SpaceBetween size="xs">
      <StatusIndicator type={readinessStatus(status)}>
        {readinessLabel(status)}{result?.database_version ? `, ${result.database_version}` : ""}
      </StatusIndicator>
      <Checklist checks={checks} />
      {databaseId && <Link href={dbHref(databaseId)} onFollow={(e) => follow(e)}>Open the database</Link>}
    </SpaceBetween>
  );
}
