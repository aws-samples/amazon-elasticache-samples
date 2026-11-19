import { useState } from "react";
import {
  Alert, Box, Button, Checkbox, ColumnLayout, FormField, Input, Modal, SpaceBetween,
} from "@cloudscape-design/components";
import { useRunDatabase } from "../api/hooks";
import type { Capabilities, DatabaseView } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { runErrors, runRequest, type RunFields } from "../databases/connection";
import { assessmentRoute, navigate } from "../router";
import { defaultOptions, JobOptionsForm, optionErrors, optionFields, type OptionValues } from "./JobOptionsForm";

/** Start an assessment of a saved database: the run options only, never the connection. */
export function RunModal({ caps, db, onClose }: { caps: Capabilities; db: DatabaseView; onClose: () => void }) {
  const L = caps.limits;
  const readiness = db.readiness?.status ?? "unchecked";
  const [f, setF] = useState<RunFields>({ window_minutes: "15", start_at: "", allow_degraded: false });
  const [opts, setOpts] = useState<OptionValues>(() => defaultOptions(caps));
  const [label, setLabel] = useState("");
  const [touched, setTouched] = useState(false);
  const run = useRunDatabase();
  const errs = { ...runErrors(f, caps, readiness), ...optionErrors(caps, opts, label) };
  const shown = touched ? errs : {};

  const submit = () => {
    setTouched(true);
    if (Object.keys(errs).length) return;
    run.mutate({ databaseId: db.database_id, body: runRequest(f, optionFields(opts, label)) }, {
      onSuccess: (r) => { onClose(); navigate(assessmentRoute(r.job_id, db.database_id)); },
    });
  };

  return (
    <Modal
      visible
      size="large"
      onDismiss={onClose}
      header={`Run an assessment of ${db.label}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onClose}>Cancel</Button>
            <Button variant="primary" loading={run.isPending} onClick={submit}>Run assessment</Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        <Box>The tool reads the database statistics at the start and the end of the window, so pick a period of normal or peak load. The saved connection is used as it is.</Box>
        <ColumnLayout columns={2}>
          <FormField label="Window (minutes)" errorText={shown.window_minutes}
            constraintText={`How long to measure, ${L.window_minutes_min} to ${L.window_minutes_max} minutes.`}>
            <Input type="number" value={f.window_minutes} onChange={({ detail }) => setF((x) => ({ ...x, window_minutes: detail.value }))} />
          </FormField>
          <FormField label="Start at" errorText={shown.start_at}
            constraintText={`Optional, your local time, up to ${L.window_start_max_hours} hours ahead. Leave empty to start at once.`}>
            <Input value={f.start_at} placeholder="YYYY-MM-DD HH:MM" onChange={({ detail }) => setF((x) => ({ ...x, start_at: detail.value }))} />
          </FormField>
        </ColumnLayout>
        <JobOptionsForm caps={caps} values={opts} onChange={setOpts} label={label} onLabel={setLabel} errors={shown} />
        {readiness === "degraded" && (
          <Alert type="warning" header="pg_stat_statements is not usable">
            <SpaceBetween size="s">
              <Box>The assessment would run in degraded mode and see less of the workload.</Box>
              <FormField errorText={shown.allow_degraded}>
                <Checkbox checked={f.allow_degraded} onChange={({ detail }) => setF((x) => ({ ...x, allow_degraded: detail.checked }))}>
                  Run in degraded mode
                </Checkbox>
              </FormField>
            </SpaceBetween>
          </Alert>
        )}
        {run.error ? <ErrorAlert error={run.error} header="The assessment was not started" /> : null}
      </SpaceBetween>
    </Modal>
  );
}
