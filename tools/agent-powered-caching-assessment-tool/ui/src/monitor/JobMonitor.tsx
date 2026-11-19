import { useEffect, useState } from "react";
import {
  Alert, Box, Container, Header, KeyValuePairs, SpaceBetween, StatusIndicator, Steps,
  type StatusIndicatorProps, type StepsProps,
} from "@cloudscape-design/components";
import type { JobStatus, PhaseInfo, PhaseStatus } from "../api/types";
import { formatSeconds, formatTime, jobStatus, secondsBetween, stateLabel } from "../components/status";
import { BulletList } from "../report/common";

type CollectionWindow = NonNullable<JobStatus["collection_window"]>;

/** Whole seconds from now until an ISO time, at least 0; null when unknown. */
export function secondsUntil(iso: string | null | undefined, now: number): number | null {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? null : Math.max(0, Math.round((t - now) / 1000));
}

/** What the collection window is doing, in one sentence. */
export function windowLine(w: CollectionWindow, now: number): string {
  const end = formatTime(w.ends_at);
  if (w.state === "waiting") {
    const left = secondsUntil(w.starts_at, now);
    return `Waiting to start the ${w.window_minutes ?? ""}-minute collection window at ${formatTime(w.starts_at)}`
      + (left !== null ? `, in ${formatSeconds(left)}.` : ".");
  }
  if (w.state === "measuring") {
    const left = secondsUntil(w.ends_at, now);
    return `Measuring the database until ${end}` + (left !== null ? `, ${formatSeconds(left)} left.` : ".");
  }
  return "The collection window has ended. Reading the statistics.";
}

function WindowAlert({ w }: { w: CollectionWindow }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <Alert type="info" header="Collection window">
      {windowLine(w, now)} Cancelling stops it within 15 seconds.
    </Alert>
  );
}

/** The latest record of a phase in the job's latest run. */
function latest(phases: ReadonlyArray<PhaseStatus>, phase: string, run: number | null | undefined): PhaseStatus | undefined {
  const mine = phases.filter((p) => p.phase === phase && (run === null || run === undefined || p.run === run || p.run === null || p.run === undefined));
  return mine.at(-1);
}

function phaseStep(info: PhaseInfo, s: JobStatus): StepsProps.Step {
  const rec = latest(s.phases ?? [], info.phase, s.run);
  const requested = (s.requested ?? []).includes(info.phase);
  const have = (s.available ?? []).includes(info.phase);
  let status: StatusIndicatorProps.Type;
  let text: string;
  if (rec?.status === "complete") { status = "success"; text = "Complete"; }
  else if (rec?.status === "failed") { status = "error"; text = "Failed"; }
  else if (rec?.status === "running") {
    status = "in-progress";
    text = s.cancel_requested ? "Running, stopping after this phase"
      : info.phase === "collect" && s.collection_window?.state === "waiting" ? "Waiting for the window"
      : info.phase === "collect" && s.collection_window?.state === "measuring" ? "Measuring" : "Running";
  } else if (!requested) { status = have ? "success" : "not-started"; text = have ? "From an earlier run" : "Not requested"; }
  else if (s.status === "cancelled") { status = "stopped"; text = "Not run (cancelled)"; }
  else if (s.status === "failed") { status = "stopped"; text = "Not run"; }
  else { status = "pending"; text = "Waiting"; }

  const took = rec ? secondsBetween(rec.started_at, rec.ended_at ?? null) : null;
  return {
    status,
    statusIconAriaLabel: text,
    header: <Box variant="span" fontWeight="bold">{info.label}</Box>,
    details: (
      <SpaceBetween size="xxs">
        <StatusIndicator type={status}>{text}</StatusIndicator>
        {rec && <Box variant="small">Started {formatTime(rec.started_at)}{took !== null ? ` · ${formatSeconds(took)}` : ""}</Box>}
        {rec?.error && <Box variant="small" color="text-status-error">{rec.error}</Box>}
        <Box variant="small" color="text-body-secondary">{info.description}</Box>
      </SpaceBetween>
    ),
  };
}

export function JobMonitor({ status, phases }: { status: JobStatus; phases: ReadonlyArray<PhaseInfo> }) {
  const s = status;
  return (
    <Container header={<Header variant="h2" description="Updates on its own while the job runs.">Pipeline</Header>}>
      <SpaceBetween size="l">
        <Steps orientation="horizontal" steps={phases.map((p) => phaseStep(p, s))} ariaLabel="Assessment phases" />
        <KeyValuePairs
          columns={4}
          items={[
            { label: "Status", value: (
              <StatusIndicator type={s.cancel_requested && s.status !== "cancelled" ? "in-progress" : jobStatus(s.status)}>
                {s.cancel_requested && s.status !== "cancelled" ? `${stateLabel(s.status)}, cancel requested` : stateLabel(s.status)}
              </StatusIndicator>
            ) },
            { label: "Current phase", value: s.current_phase ?? "-" },
            { label: "Run", value: s.run === null || s.run === undefined ? "-" : String(s.run) },
            { label: "Duration", value: formatSeconds(s.duration_seconds) },
            { label: "Created", value: formatTime(s.created_at) },
            { label: "Started", value: formatTime(s.started_at) },
            { label: "Completed", value: formatTime(s.completed_at) },
            { label: "Artifacts", value: (s.available ?? []).join(", ") || "None yet" },
          ]}
        />
        {s.status === "running" && s.current_phase === "collect" && s.collection_window && (
          <WindowAlert w={s.collection_window} />
        )}
        {s.error && <Alert type="error" header="The job failed">{s.error}</Alert>}
        {s.note && <Alert type="info" header="Cancelled">{s.note}</Alert>}
        {(s.warnings ?? []).length > 0 && (
          <Alert type="warning" header="Warnings"><BulletList items={s.warnings ?? []} label="Job warnings" /></Alert>
        )}
      </SpaceBetween>
    </Container>
  );
}
