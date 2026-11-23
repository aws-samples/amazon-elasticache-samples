import { useState } from "react";
import {
  Alert, Box, Button, ColumnLayout, Container, ContentLayout, Header, Link,
  SpaceBetween, Table,
} from "@cloudscape-design/components";
import { useDatabases, useDiscovery } from "../api/hooks";
import type { DiscoveredServer } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { pssLabel, sourceLabel, splitFinds } from "../databases/connection";
import { HostProbePanel } from "../databases/HostProbePanel";
import { follow, href } from "../router";

const STEPS = [
  { title: "1. Add your database", text: "Tell the tool the SSH host and the database. The tool can create a read-only user there for you, or use one you have. The password stays on the server or in Secrets Manager." },
  { title: "2. Check readiness", text: "The tool checks SSH, the sign-in and pg_stat_statements, and says what to fix before you run anything." },
  { title: "3. Run an assessment", text: "The tool measures the workload for a window you pick, then says which queries and tables to cache and what the cache would cost." },
  { title: "4. Ask the assistant", text: "Ask the AI assistant why a query or table was recommended or ruled out. It answers from the assessment's own numbers and can start the next run for you." },
];

/** Get started: the steps, the databases (or instances) found, and a host to probe. */
export function StartPage() {
  const disc = useDiscovery();
  const dbs = useDatabases();
  const [selected, setSelected] = useState<DiscoveredServer[]>([]);
  const { databases } = splitFinds(disc.data?.servers ?? []);
  const sel = selected[0];
  const saved = dbs.data?.total ?? 0;

  return (
    <ContentLayout header={
      <Header variant="h1"
        description="Find out whether a self-managed PostgreSQL database would gain from an Amazon ElastiCache for Valkey cache.">
        Get started
      </Header>
    }>
      <SpaceBetween size="l">
        <Container header={<Header variant="h2">How it works</Header>}>
          <ColumnLayout columns={4} variant="text-grid">
            {STEPS.map((s) => (
              <SpaceBetween key={s.title} size="xxs">
                <Box variant="h3">{s.title}</Box>
                <Box color="text-body-secondary">{s.text}</Box>
              </SpaceBetween>
            ))}
          </ColumnLayout>
        </Container>
        {saved > 0 && (
          <Alert type="info" action={<Button href={href({ name: "databases" })} onFollow={(e) => follow(e)}>View databases</Button>}>
            {saved === 1 ? "One database is saved." : `${saved} databases are saved.`} Open one to run an assessment.
          </Alert>
        )}
        {disc.error ? <ErrorAlert error={disc.error} header="Discovery could not run" onRetry={() => void disc.refetch()} /> : null}

        <Table<DiscoveredServer>
          variant="container"
          items={databases}
          loading={disc.isFetching && !disc.data}
          loadingText="Looking for databases"
          trackBy="id"
          selectionType="single"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
          ariaLabels={{
            selectionGroupLabel: "Databases",
            itemSelectionLabel: (_s, i) => `Select ${i.label}`,
            tableLabel: "Databases",
          }}
          header={
            <Header
              counter={disc.data ? `(${databases.length})` : undefined}
              description="Databases found by probing a host below, when the SSH user can run sudo as postgres, and PostgreSQL instances on this machine or in pg_service.conf. Pick one to add it."
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" loading={disc.isFetching} onClick={() => void disc.refetch()}>Look again</Button>
                  {/* One button: empty form with nothing picked, filled in from the pick otherwise. */}
                  <Button variant="primary" href={href(sel ? { name: "connect", pick: sel.id } : { name: "connect" })}
                    onFollow={(e) => follow(e)}>{!sel ? "Add new database" : sel.database ? "Add this database" : "Add database from this instance"}</Button>
                </SpaceBetween>
              }
            >
              Databases
            </Header>
          }
          empty={
            <Box textAlign="center" color="inherit">
              <SpaceBetween size="xxs">
                <Box variant="strong">No databases found</Box>
                <Box color="text-body-secondary">Probe the database host below to look for PostgreSQL on it.</Box>
              </SpaceBetween>
            </Box>
          }
          wrapLines
          columnDefinitions={[
            { id: "label", header: "Database or instance", cell: (s) => (
              <SpaceBetween size="xxs">
                <span>{s.label}</span>
                {(s.notes ?? []).map((n, i) => <Box key={i} variant="small" color="text-body-secondary">{n}</Box>)}
              </SpaceBetween>
            ) },
            { id: "source", header: "Found by", width: 160, cell: (s) => sourceLabel(s.source) },
            { id: "version", header: "Version", width: 110, cell: (s) => s.version ?? "-" },
            { id: "pss", header: "pg_stat_statements", width: 170, cell: (s) => pssLabel(s.pg_stat_statements) },
          ]}
        />

        <HostProbePanel />

        <Box color="text-body-secondary">
          Not listed?{" "}
          <Link href={href({ name: "connect" })} onFollow={(e) => follow(e)}>Enter the connection details yourself</Link>
        </Box>
      </SpaceBetween>
    </ContentLayout>
  );
}
