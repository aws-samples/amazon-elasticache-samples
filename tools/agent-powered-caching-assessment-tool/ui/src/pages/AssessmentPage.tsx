import { useEffect } from "react";
import {
  Alert, Box, Button, ContentLayout, ExpandableSection, Header, SpaceBetween, Spinner,
} from "@cloudscape-design/components";
import { useQueryClient } from "@tanstack/react-query";
import { htmlReportUrl } from "../api/client";
import { keys, useCancel, useCapabilities, useJobStatus } from "../api/hooks";
import { useAgent, WORKLOAD } from "../agent/AgentContext";
import { ErrorAlert } from "../components/ErrorAlert";
import { useNotify } from "../components/Notifications";
import { stateLabel } from "../components/status";
import { JobMonitor } from "../monitor/JobMonitor";
import { ReportView } from "../report/ReportView";

const ACTIVE = new Set(["queued", "running"]);

export function AssessmentPage({ jobId }: { jobId: string }) {
  const caps = useCapabilities();
  const st = useJobStatus(jobId);
  const cancel = useCancel();
  const notify = useNotify();
  const agent = useAgent();
  const qc = useQueryClient();
  const s = st.data;
  const active = s ? ACTIVE.has(s.status) : false;
  const hasAnalysis = !!s?.available?.includes("analyze");
  const hasHtml = !!s?.available?.includes("report");

  // When a job finishes, drop any report data cached while it was still running.
  useEffect(() => {
    if (s && !active) {
      void qc.invalidateQueries({ queryKey: keys.overview(jobId) });
      void qc.invalidateQueries({ queryKey: keys.summary(jobId) });
    }
  }, [s?.status, active, jobId, qc]); // eslint-disable-line react-hooks/exhaustive-deps

  const onCancel = () =>
    cancel.mutate(jobId, {
      onSuccess: (r) => notify({ type: "info", content: r.message }),
      onError: (e) => notify({ type: "error", header: "Cancel failed", content: <ErrorAlert error={e} /> }),
    });

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={s ? `Status: ${stateLabel(s.status)}${s.cancel_requested && s.status !== "cancelled" ? " (cancel requested)" : ""}` : undefined}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {active && (
                <Button onClick={onCancel} loading={cancel.isPending} disabled={!!s?.cancel_requested}>
                  {s?.cancel_requested ? "Stopping" : "Cancel assessment"}
                </Button>
              )}
              {hasHtml && (
                <Button href={htmlReportUrl(jobId)} target="_blank" iconName="external" iconAlign="right"
                  ariaLabel="Open the HTML report in a new tab">
                  HTML report
                </Button>
              )}
              {hasAnalysis && !active && (
                <Button variant="primary" iconName="gen-ai" onClick={() => agent.ask(WORKLOAD)}>Ask about this report</Button>
              )}
            </SpaceBetween>
          }
        >
          {jobId}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {st.error ? <ErrorAlert error={st.error} header="Could not read the job" onRetry={() => void st.refetch()} /> : null}
        {caps.error ? <ErrorAlert error={caps.error} header="Server capabilities could not be loaded" /> : null}
        {st.isLoading && <Box textAlign="center" padding="xl"><Spinner size="large" /></Box>}
        {s && caps.data && (
          active || s.status !== "complete"
            ? <JobMonitor status={s} phases={caps.data.phases} />
            : (
              <ExpandableSection variant="container" headerText="Run details" headerDescription="Phases, timings and warnings">
                <JobMonitor status={s} phases={caps.data.phases} />
              </ExpandableSection>
            )
        )}
        {s && !active && s.status === "complete" && (s.warnings ?? []).length > 0 && (
          <Alert type="warning" header="This assessment finished with warnings">
            <ul style={{ margin: 0, paddingInlineStart: "1.25em" }}>{(s.warnings ?? []).map((w, i) => <li key={i}>{w}</li>)}</ul>
          </Alert>
        )}
        {s && !active && hasAnalysis && <ReportView jobId={jobId} />}
        {s && !active && !hasAnalysis && s.status === "complete" && (
          <Box color="text-body-secondary">This job has no analysis, so there is no report to show. It holds: {(s.available ?? []).join(", ") || "nothing"}.</Box>
        )}
      </SpaceBetween>
    </ContentLayout>
  );
}
