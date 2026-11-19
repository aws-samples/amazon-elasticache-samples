import { useMemo, useState } from "react";
import {
  Box, Button, ContentLayout, Header, Link, SpaceBetween, StatusIndicator, Table, TextFilter,
} from "@cloudscape-design/components";
import { useDatabases } from "../api/hooks";
import type { DatabaseView } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { formatTime, jobStatus, readinessLabel, readinessStatus, stateLabel, verdictStatus } from "../components/status";
import { assessmentRoute, follow, href } from "../router";
import { DeleteDatabaseModal } from "./DatabasePage";

/**
 * Whether a database matches the find text.
 * @param d The database.
 * @param text The find text, lower case.
 * @returns True when the label, database name or SSH target contains it.
 */
function matches(d: DatabaseView, text: string): boolean {
  if (!text) return true;
  return [d.label, d.connection.database_name, d.connection.ssh_target].some((v) => v.toLowerCase().includes(text));
}

/** The saved databases: readiness, latest verdict and assessment count. */
export function DatabasesPage() {
  const dbs = useDatabases();
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<DatabaseView[]>([]);
  const items = useMemo(() => (dbs.data?.items ?? []).filter((d) => matches(d, text.trim().toLowerCase())), [dbs.data, text]);
  const sel = selected[0];
  const [deleting, setDeleting] = useState<DatabaseView | null>(null);
  const selBusy = (sel?.running ?? 0) > 0;

  return (
    <ContentLayout header={<Header variant="h1" description="Saved database connections, newest first. Open one to check it or run an assessment.">Databases</Header>}>
      <SpaceBetween size="l">
        {dbs.error ? <ErrorAlert error={dbs.error} header="The databases could not be loaded" onRetry={() => void dbs.refetch()} /> : null}
        <Table<DatabaseView>
          variant="full-page"
          items={items}
          loading={dbs.isLoading}
          loadingText="Loading databases"
          trackBy="database_id"
          selectionType="single"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
          ariaLabels={{
            selectionGroupLabel: "Database selection",
            itemSelectionLabel: (_s, d) => `Select ${d.label}`,
            tableLabel: "Databases",
          }}
          header={
            <Header
              counter={dbs.data ? `(${dbs.data.total})` : undefined}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button disabled={!sel} href={sel ? href({ name: "database", id: sel.database_id }) : undefined}
                    onFollow={(e) => follow(e)}>Open</Button>
                  <Button disabled={!sel || selBusy} onClick={() => sel && setDeleting(sel)}
                    ariaLabel={selBusy ? "Delete (an assessment is still running)" : "Delete"}>Delete</Button>
                  <Button variant="primary" href={href({ name: "connect" })} onFollow={(e) => follow(e)}>Add a database</Button>
                </SpaceBetween>
              }
            >
              Databases
            </Header>
          }
          filter={
            <TextFilter filteringText={text} filteringPlaceholder="Find by label, database or host"
              filteringAriaLabel="Find databases" onChange={({ detail }) => setText(detail.filteringText)}
              countText={text ? `${items.length} match${items.length === 1 ? "" : "es"}` : undefined} />
          }
          empty={
            <Box textAlign="center" color="inherit">
              <SpaceBetween size="s">
                <Box variant="strong">No saved databases</Box>
                <Button href={href({ name: "connect" })} onFollow={(e) => follow(e)}>Add a database</Button>
              </SpaceBetween>
            </Box>
          }
          wrapLines={false}
          columnDefinitions={[
            { id: "label", header: "Database", width: 220, cell: (d) => (
              <Link href={href({ name: "database", id: d.database_id })} onFollow={(e) => follow(e)}>{d.label}</Link>
            ) },
            { id: "target", header: "Where", width: 220, cell: (d) => `${d.connection.database_name} on ${d.connection.ssh_target}` },
            { id: "readiness", header: "Readiness", width: 200, cell: (d) => (
              <StatusIndicator type={readinessStatus(d.readiness?.status)}>{readinessLabel(d.readiness?.status)}</StatusIndicator>
            ) },
            { id: "verdict", header: "Latest verdict", width: 200, cell: (d) => d.latest_complete
              ? (
                <Link href={href(assessmentRoute(d.latest_complete.job_id, d.database_id))} onFollow={(e) => follow(e)}>
                  <StatusIndicator type={verdictStatus(d.latest_complete.workload_verdict)}>{d.latest_complete.workload_verdict ?? "No verdict"}</StatusIndicator>
                </Link>
              )
              : "-" },
            { id: "latest", header: "Latest assessment", width: 190, cell: (d) => d.latest
              ? <StatusIndicator type={jobStatus(d.latest.status)}>{stateLabel(d.latest.status)}, {formatTime(d.latest.created_at)}</StatusIndicator>
              : "-" },
            { id: "count", header: "Assessments", width: 130, cell: (d) => `${d.assessments ?? 0}${(d.running ?? 0) > 0 ? ` (${d.running} running)` : ""}` },
            { id: "checked", header: "Checked", width: 140, cell: (d) => formatTime(d.readiness?.checked_at) },
          ]}
        />
      </SpaceBetween>
      {deleting && (
        <DeleteDatabaseModal db={deleting} stay onClose={() => { setDeleting(null); setSelected([]); }} />
      )}
    </ContentLayout>
  );
}
