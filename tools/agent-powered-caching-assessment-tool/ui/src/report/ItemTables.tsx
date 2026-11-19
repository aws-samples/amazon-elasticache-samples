import { useEffect, useState } from "react";
import {
  Badge, Box, Container, Header, Pagination, SegmentedControl, SpaceBetween, Spinner,
} from "@cloudscape-design/components";
import { useQueryPage, useTablePage } from "../api/hooks";
import type { MetricSet, NotCacheableGroup, QueryItem, TableItem } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { ScoreCell, Shown, Verdict } from "./common";
import { ExpandableList, type ListColumn } from "./ExpandableList";
import { QueryDetail, TableDetail } from "./ItemDetail";

// Both lists page through the server's report order and do not re-sort it: the order is
// the report's own (verdict, then database time). Each row opens its card in place.

// Fixed widths. The verdict gets enough room for "Moderate Candidate" on one line.
const VERDICT_W = "190px";
const METRIC_W = "68px";

function metricCols<T>(get: (i: T) => MetricSet, idle?: (i: T) => boolean): ListColumn<T>[] {
  const quiet = (i: T) => idle?.(i) ?? false;
  const cell = (k: "reads_per_sec" | "avg_ms" | "db_ms_per_sec" | "writes_per_sec" | "read_dominance") =>
    (i: T) => (quiet(i) ? <Shown value={null} missing="-" /> : <Shown value={get(i).display[k]} missing="-" />);
  return [
    { id: "reads", header: "Reads/s", width: METRIC_W, align: "end", cell: cell("reads_per_sec") },
    { id: "avg", header: "Avg ms", width: METRIC_W, align: "end", cell: cell("avg_ms") },
    { id: "db", header: "DB ms/s", width: METRIC_W, align: "end", cell: cell("db_ms_per_sec") },
    { id: "writes", header: "Writes/s", width: METRIC_W, align: "end", cell: cell("writes_per_sec") },
    { id: "rd", header: "Read dom.", width: METRIC_W, align: "end", cell: cell("read_dominance") },
    { id: "score", header: "Score", width: "116px", align: "end", cell: (i) => <ScoreCell m={get(i)} /> },
  ];
}

function useResetOnChange(dep: unknown, reset: () => void) {
  useEffect(reset, [dep]); // eslint-disable-line react-hooks/exhaustive-deps
}

export function QueriesSection({ jobId, pageSize, lead, note, notCacheable }: {
  jobId: string; pageSize: number; lead?: string | null; note?: string | null;
  notCacheable: NotCacheableGroup[];
}) {
  const [filter, setFilter] = useState<"all" | "recommended">("all");
  const [page, setPage] = useState(1);
  useResetOnChange(filter, () => setPage(1));
  const q = useQueryPage(jobId, {
    limit: pageSize, offset: (page - 1) * pageSize,
    ...(filter === "recommended" ? { recommended: true } : {}),
  });
  const total = q.data?.total ?? 0;
  const columns: ListColumn<QueryItem>[] = [
    { id: "rank", header: "#", width: "28px", cell: (i) => i.rank },
    { id: "verdict", header: "Verdict", width: VERDICT_W, cell: (i) => (
      <Verdict verdict={i.verdict} label={i.is_candidate ? i.verdict_label : "Not cacheable"}
        m={i.is_candidate ? i.metrics : undefined} byModel={i.judged_by_model} />
    ) },
    { id: "sql", header: "Type and query", width: "minmax(160px, 1fr)", cell: (i) => (
      <span title={i.sql_short}>
        <Badge color={i.cache_type === "QUERY" || i.cache_type === "OBJECT" ? "blue" : "grey"}>{i.cache_type}</Badge>{" "}
        <Box variant="code" fontSize="body-s">{i.sql_short}</Box>
      </span>
    ) },
    ...metricCols<QueryItem>((i) => i.metrics),
  ];

  return (
    <Container
      header={
        <Header
          counter={q.data ? `(${total})` : undefined}
          description={<>{lead ? <>{lead} </> : null}Expand a row to see the query&apos;s full card.</>}
          actions={
            <SegmentedControl
              label="Show"
              selectedId={filter}
              onChange={({ detail }) => setFilter(detail.selectedId === "recommended" ? "recommended" : "all")}
              options={[{ id: "all", text: "All" }, { id: "recommended", text: "Recommended" }]}
            />
          }
        >
          Queries
        </Header>
      }
      footer={
        <Box float="right">
          <Pagination currentPageIndex={page} pagesCount={Math.max(1, Math.ceil(total / pageSize))}
            onChange={({ detail }) => setPage(detail.currentPageIndex)} />
        </Box>
      }
    >
      <SpaceBetween size="s">
        {q.error ? <ErrorAlert error={q.error} onRetry={() => void q.refetch()} /> : null}
        {q.isLoading && <Box textAlign="center"><Spinner /> Loading queries</Box>}
        <ExpandableList<QueryItem>
          key={`${filter}-${page}`}
          items={q.data?.items ?? []}
          columns={columns}
          getId={(i) => i.queryid}
          rowLabel={(i) => `query ${i.rank}, ${i.sql_short}`}
          renderDetail={(i) => <QueryDetail q={i} notCacheable={notCacheable} />}
          loading={q.isLoading}
          empty={note ?? "No queries match."}
        />
      </SpaceBetween>
    </Container>
  );
}

type TableFilter = "all" | "recommended" | "ruled_out";

export function TablesSection({ jobId, pageSize }: { jobId: string; pageSize: number }) {
  const [filter, setFilter] = useState<TableFilter>("all");
  const [page, setPage] = useState(1);
  useResetOnChange(filter, () => setPage(1));
  const q = useTablePage(jobId, {
    limit: pageSize, offset: (page - 1) * pageSize,
    ...(filter === "recommended" ? { recommended: true } : {}),
    ...(filter === "ruled_out" ? { ruled_out: true } : {}),
  });
  const total = q.data?.total ?? 0;
  const columns: ListColumn<TableItem>[] = [
    { id: "verdict", header: "Verdict", width: VERDICT_W, cell: (i) => (
      <Verdict verdict={i.verdict} label={i.verdict_label} m={i.metrics} byModel={i.judged_by_model} />
    ) },
    { id: "table", header: "Table", width: "minmax(160px, 1fr)", cell: (i) => <span title={i.table_id}>{i.table_id}</span> },
    ...metricCols<TableItem>((i) => i.metrics, (i) => i.idle),
  ];

  return (
    <Container
      header={
        <Header
          counter={q.data ? `(${total})` : undefined}
          description="Object caching, per table. † A table's DB ms/s includes joined queries, so tables overlap. Expand a row to see the table's full card."
          actions={
            <SegmentedControl
              label="Show"
              selectedId={filter}
              onChange={({ detail }) => setFilter(detail.selectedId as TableFilter)}
              options={[
                { id: "all", text: "All" },
                { id: "recommended", text: "Recommended" },
                { id: "ruled_out", text: "Ruled out" },
              ]}
            />
          }
        >
          Tables
        </Header>
      }
      footer={
        <Box float="right">
          <Pagination currentPageIndex={page} pagesCount={Math.max(1, Math.ceil(total / pageSize))}
            onChange={({ detail }) => setPage(detail.currentPageIndex)} />
        </Box>
      }
    >
      <SpaceBetween size="s">
        {q.error ? <ErrorAlert error={q.error} onRetry={() => void q.refetch()} /> : null}
        {q.isLoading && <Box textAlign="center"><Spinner /> Loading tables</Box>}
        <ExpandableList<TableItem>
          key={`${filter}-${page}`}
          items={q.data?.items ?? []}
          columns={columns}
          getId={(i) => i.table_id}
          rowLabel={(i) => `table ${i.table_id}`}
          renderDetail={(i) => <TableDetail t={i} />}
          loading={q.isLoading}
          empty="No tables match."
        />
      </SpaceBetween>
    </Container>
  );
}
