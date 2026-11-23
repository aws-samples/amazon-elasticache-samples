import { useEffect, useMemo, useState } from "react";
import {
  Box, Button, Header, Link, Modal, Pagination, Select,
  SpaceBetween, StatusIndicator, Table, TextFilter, type SelectProps,
} from "@cloudscape-design/components";
import { useCapabilities, useDatabases, useDelete, useJobList, useRerun } from "../api/hooks";
import type { Capabilities, JobState, JobSummary } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { useNotify } from "../components/Notifications";
import { formatSeconds, formatTime, jobStatus, stateLabel } from "../components/status";
import { assessmentRoute, follow, href, navigate } from "../router";
import { defaultOptions, JobOptionsForm, optionErrors, optionFields, type OptionValues } from "./JobOptionsForm";

const ANY: SelectProps.Option = { value: "", label: "Any status" };

function RerunModal({ caps, job, onClose }: { caps: Capabilities; job: JobSummary; onClose: () => void }) {
  const [opts, setOpts] = useState<OptionValues>(() => defaultOptions(caps, job.options ?? {}));
  const [label, setLabel] = useState(job.label ? `${job.label} (rerun)` : "");
  const rerun = useRerun();
  const errs = optionErrors(caps, opts, label);
  return (
    <Modal
      visible
      onDismiss={onClose}
      header={`Rerun ${job.job_id}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={rerun.isPending} disabled={Object.keys(errs).length > 0}
              onClick={() => rerun.mutate({ jobId: job.job_id, body: optionFields(opts, label) }, {
                onSuccess: (r) => { onClose(); navigate({ name: "assessment", id: r.job_id }); },
              })}>
              Rerun
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>Starts a new job on the collection this job saved. The original is not changed.</Box>
        <JobOptionsForm caps={caps} values={opts} onChange={setOpts} label={label} onLabel={setLabel} errors={errs} />
        {rerun.error ? <ErrorAlert error={rerun.error} header="Rerun failed" /> : null}
      </SpaceBetween>
    </Modal>
  );
}

function DeleteModal({ job, onClose }: { job: JobSummary; onClose: () => void }) {
  const del = useDelete();
  const notify = useNotify();
  return (
    <Modal
      visible
      onDismiss={onClose}
      header="Delete assessment"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={del.isPending}
              onClick={() => del.mutate(job.job_id, {
                onSuccess: () => { notify({ type: "success", content: `Deleted ${job.job_id}.` }); onClose(); },
              })}>
              Delete
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>Delete <Box variant="strong">{job.job_id}</Box> and every artifact it holds? This cannot be undone.</Box>
        {del.error ? <ErrorAlert error={del.error} header="Delete failed" /> : null}
      </SpaceBetween>
    </Modal>
  );
}

/** Every assessment on the server: the all-assessments list. The table header is the page title. */
export function HistoryPage() {
  return <AssessmentsTable />;
}

/**
 * The assessments table, with find, status filter, open, rerun and delete. With
 * databaseId it lists that saved database's assessments only (the database page).
 */
export function AssessmentsTable({ databaseId }: { databaseId?: string }) {
  const caps = useCapabilities();
  const dbs = useDatabases();
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState<SelectProps.Option>(ANY);
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<JobSummary[]>([]);
  const [modal, setModal] = useState<"rerun" | "delete" | null>(null);
  const limit = caps.data?.limits.list_default_limit ?? 0;

  useEffect(() => {
    const t = window.setTimeout(() => { setQ(text.trim()); setPage(1); }, 300);
    return () => window.clearTimeout(t);
  }, [text]);

  const query = useMemo(() => ({
    limit, offset: (page - 1) * limit,
    ...(q ? { q } : {}),
    ...(status.value ? { status: status.value as JobState } : {}),
    ...(databaseId ? { database_id: databaseId } : {}),
  }), [limit, page, q, status, databaseId]);
  const list = useJobList(query, true);
  const items = list.data?.items ?? [];
  const sel = selected[0];
  const statusOptions: SelectProps.Option[] = [ANY, ...(caps.data?.job_states ?? []).map((s) => ({ value: s, label: stateLabel(s) }))];
  const labels = useMemo(() => new Map((dbs.data?.items ?? []).map((d) => [d.database_id, d.label])), [dbs.data]);
  const jobHref = (j: JobSummary) => href(assessmentRoute(j.job_id, j.database_id));

  return (
    <>
      <SpaceBetween size="l">
        {caps.error ? <ErrorAlert error={caps.error} header="Server capabilities could not be loaded" onRetry={() => void caps.refetch()} /> : null}
        {list.error ? <ErrorAlert error={list.error} header="The list could not be loaded" onRetry={() => void list.refetch()} /> : null}
        <Table<JobSummary>
          variant={databaseId ? "container" : "full-page"}
          items={items}
          loading={list.isLoading || caps.isLoading}
          loadingText="Loading assessments"
          trackBy="job_id"
          selectionType="single"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
          ariaLabels={{
            selectionGroupLabel: "Assessment selection",
            itemSelectionLabel: (_s, i) => `Select ${i.job_id}`,
            tableLabel: "Assessments",
          }}
          header={
            <Header
              counter={list.data ? `(${list.data.total})` : undefined}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button disabled={!sel} href={sel ? jobHref(sel) : undefined}
                    onFollow={(e) => follow(e)}>Open</Button>
                  <Button disabled={!sel || !caps.data?.features.rerun || !sel.available?.includes("collect")}
                    onClick={() => setModal("rerun")}>Rerun</Button>
                  <Button disabled={!sel || !caps.data?.features.list_and_delete} onClick={() => setModal("delete")}>Delete</Button>
                  {!databaseId && <Button variant="primary" href={href({ name: "new" })} onFollow={(e) => follow(e)}>New assessment</Button>}
                </SpaceBetween>
              }
              variant={databaseId ? "h2" : "awsui-h1-sticky"}
              description={databaseId ? undefined : "Every assessment on this server, newest first."}
            >
              {databaseId ? "Assessments of this database" : "Assessments"}
            </Header>
          }
          filter={
            <SpaceBetween direction="horizontal" size="xs">
              <TextFilter filteringText={text} filteringPlaceholder="Find by id, label or database"
                filteringAriaLabel="Find assessments" onChange={({ detail }) => setText(detail.filteringText)} />
              <Select selectedOption={status} options={statusOptions} ariaLabel="Filter by status"
                onChange={({ detail }) => { setStatus(detail.selectedOption); setPage(1); }} />
            </SpaceBetween>
          }
          pagination={
            <Pagination currentPageIndex={page}
              pagesCount={Math.max(1, Math.ceil((list.data?.total ?? 0) / Math.max(1, limit)))}
              onChange={({ detail }) => setPage(detail.currentPageIndex)} />
          }
          empty={
            <Box textAlign="center" color="inherit">
              <SpaceBetween size="s">
                <Box variant="strong">{databaseId ? "No assessments of this database yet" : "No assessments"}</Box>
                {!databaseId && <Button href={href({ name: "new" })} onFollow={(e) => follow(e)}>New assessment</Button>}
              </SpaceBetween>
            </Box>
          }
          wrapLines={false}
          columnDefinitions={[
            { id: "id", header: "Assessment", width: 145, cell: (j) => (
              <Link href={jobHref(j)} onFollow={(e) => follow(e)}>{j.label ?? j.job_id}</Link>
            ) },
            { id: "status", header: "Status", width: 170, cell: (j) => {
              const st = j.status ?? "queued";
              const warn = st === "complete" && (j.warnings ?? []).length > 0;
              return <StatusIndicator type={warn ? "warning" : jobStatus(st)}>{warn ? "Complete, with warnings" : stateLabel(st)}{j.current_phase && st === "running" ? `: ${j.current_phase}` : ""}</StatusIndicator>;
            } },
            { id: "db", header: "Database", width: 160, cell: (j) => {
              const saved = j.database_id ? labels.get(j.database_id) : undefined;
              if (saved && j.database_id) {
                return <Link href={href({ name: "database", id: j.database_id })} onFollow={(e) => follow(e)}>{saved}</Link>;
              }
              return j.database_name ?? j.input?.database_name ?? "-";
            } },
            { id: "source", header: "Source", width: 80, cell: (j) => j.source },
            { id: "verdict", header: "Workload verdict", width: 190, cell: (j) => j.workload_verdict ?? "-" },
            { id: "interp", header: "Interpretation", width: 145, cell: (j) => j.interpretation_mode ?? "-" },
            { id: "created", header: "Created", width: 135, cell: (j) => formatTime(j.created_at) },
            { id: "duration", header: "Duration", width: 95, cell: (j) => formatSeconds(j.duration_seconds) },
          ]}
        />
      </SpaceBetween>
      {modal === "rerun" && sel && caps.data && <RerunModal caps={caps.data} job={sel} onClose={() => setModal(null)} />}
      {modal === "delete" && sel && <DeleteModal job={sel} onClose={() => { setModal(null); setSelected([]); }} />}
    </>
  );
}
