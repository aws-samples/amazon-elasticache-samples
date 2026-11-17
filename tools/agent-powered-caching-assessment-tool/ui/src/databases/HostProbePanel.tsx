// "Look on a database host": type an SSH host (with suggestions), pick a key, probe it in
// stages, and trust an unknown host key after checking its fingerprint. What a probe finds
// joins the shared discovery result, so every list shows it: the databases when the SSH
// user can sudo as postgres, else the instances.
// Used on Get started, in the connect wizard, on a database page and in the assistant.
// Spec: .kiro/specs/ssh-host-probe.
import { useMemo, useState } from "react";
import {
  Alert, Autosuggest, Box, Button, ColumnLayout, Container, FormField, Header, Input, Modal,
  Select, SpaceBetween, StatusIndicator, type StatusIndicatorProps,
} from "@cloudscape-design/components";
import { useQueryClient } from "@tanstack/react-query";
import { keys as queryKeys, useHostProbe, useHostSuggestions, useSshKeys, useTrustHost } from "../api/hooks";
import type { DiscoveryResult, HostProbeResult, ProbeStage } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { mergeFinds } from "./connection";
import {
  EMPTY_PROBE, STAGE_LABEL, fieldsFromSuggestion, probeErrors, probeFinds, probeRequest, visibleSuggestions,
  type ProbeFields,
} from "./hostProbe";

const STATUS: Record<ProbeStage["status"], StatusIndicatorProps.Type> = {
  pass: "success", fail: "error", warn: "warning", skipped: "stopped",
};

/** The probe's stages as a checklist, with the fix under a failure. */
export function ProbeStages({ result }: { result: HostProbeResult }) {
  return (
    <SpaceBetween size="xs">
      {result.stages.map((s) => (
        <div key={s.name}>
          <StatusIndicator type={STATUS[s.status]}>
            <Box variant="span" fontWeight="bold">{STAGE_LABEL[s.name]}</Box>: {s.message}
          </StatusIndicator>
          {s.hint && s.status !== "pass" && s.status !== "skipped" && (
            <Box variant="small" color="text-body-secondary" padding={{ left: "l" }}>{s.hint}</Box>
          )}
        </div>
      ))}
    </SpaceBetween>
  );
}

/**
 * The probe panel.
 * @param initial Fields to start with (a saved database's host).
 * @param variant "container" on its own, "embedded" inside another container or a card.
 * @param onResult Called with each finished probe (the assistant's form uses it).
 * @param onCancel Shows a Cancel button when given.
 */
export function HostProbePanel({ initial, fixed, variant = "container", onResult, onCancel, description }: {
  initial?: Partial<ProbeFields>;
  /** Probe exactly this host, with no fields to edit (the wizard's sign-in step, a database page). */
  fixed?: ProbeFields;
  variant?: "container" | "embedded";
  onResult?: (r: HostProbeResult) => void;
  onCancel?: () => void;
  description?: string;
}) {
  const qc = useQueryClient();
  const suggestions = useHostSuggestions();
  const sshKeys = useSshKeys();
  const probe = useHostProbe();
  const trust = useTrustHost();
  const [typed, setF] = useState<ProbeFields>({ ...EMPTY_PROBE, ...initial });
  const f = fixed ?? typed;
  const [tried, setTried] = useState(false);
  const [confirmTrust, setConfirmTrust] = useState(false);
  const errors = probeErrors(f);
  const shown = tried ? errors : {};
  const keyList = sshKeys.data?.items ?? [];
  // Git and code hosts are never suggested; any host can still be typed.
  const items = useMemo(() => visibleSuggestions(suggestions.data?.items ?? [], false), [suggestions.data]);
  const result = probe.data;

  const run = () => {
    setTried(true);
    if (Object.keys(errors).length) return;
    trust.reset();
    probe.mutate(probeRequest(f), {
      onSuccess: (r) => {
        const finds = probeFinds(r);
        if (finds.length) {
          qc.setQueryData<DiscoveryResult>(queryKeys.discovery, (old) =>
            old ? { ...old, servers: mergeFinds(old.servers ?? [], finds) } : old);
        }
        onResult?.(r);
      },
    });
  };

  const doTrust = () => {
    setConfirmTrust(false);
    const fp = result?.host_key?.fingerprint;
    if (!fp) return;
    trust.mutate({ ssh_target: f.ssh_target.trim(), ssh_port: Number(f.ssh_port), fingerprint: fp }, {
      onSuccess: (t) => { if (t.trusted) run(); },
    });
  };

  const keyOptions = [
    { value: "", label: "Key from the SSH config", description: "Whatever ~/.ssh/config names for this host" },
    ...keyList.map((k) => ({
      value: k.name, label: k.name, disabled: !k.usable,
      description: k.usable ? `${k.key_type ?? "key"} ${k.fingerprint ?? ""}` : (k.problem ?? "Not usable"),
    })),
  ];
  const selectedKey = keyOptions.find((o) => o.value === f.identity_file) ?? keyOptions[0] ?? null;

  const body = (
    <SpaceBetween size="m">
      {fixed && Object.keys(errors).length > 0 && tried && (
        <Alert type="warning">Fill in a valid SSH target and port first.</Alert>
      )}
      {!fixed && <ColumnLayout columns={3}>
        <FormField label="SSH host" errorText={shown.ssh_target}
          constraintText="user@host, a host, or a name from the server's SSH config.">
          <Autosuggest
            value={f.ssh_target}
            onChange={({ detail }) => {
              const pick = items.find((s) => s.ssh_target === detail.value);
              setF(pick ? fieldsFromSuggestion(pick, keyList) : { ...f, ssh_target: detail.value });
            }}
            options={items.map((s) => ({
              value: s.ssh_target, label: s.label,
              description: s.source === "saved_database" ? "Saved database"
                : s.source === "recent_probe" ? `Probed before${s.last_result && s.last_result !== "ok" ? `, stopped at ${STAGE_LABEL[s.last_result as ProbeStage["name"]] ?? s.last_result}` : ""}`
                  : "From the SSH config",
            }))}
            enteredTextLabel={(v) => `Use: ${v}`}
            placeholder="ec2-user@203.0.113.10"
            ariaLabel="SSH host"
            empty="No suggestions"
          />
        </FormField>
        <FormField label="SSH port" errorText={shown.ssh_port}>
          <Input value={f.ssh_port} inputMode="numeric" onChange={({ detail }) => setF({ ...f, ssh_port: detail.value })} />
        </FormField>
        <FormField label="SSH key" constraintText={sshKeys.data && !sshKeys.data.configured ? "SSH_KEY_DIR is not set on the server." : undefined}>
          <Select selectedOption={selectedKey} options={keyOptions} loadingText="Loading keys"
            statusType={sshKeys.isLoading ? "loading" : "finished"}
            onChange={({ detail }) => setF({ ...f, identity_file: detail.selectedOption.value ?? "" })} />
        </FormField>
      </ColumnLayout>}
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Button variant={fixed ? "normal" : "primary"} onClick={run} loading={probe.isPending}>
          {fixed ? "Probe this host" : "Probe"}
        </Button>
        {onCancel && <Button onClick={onCancel}>Cancel</Button>}
      </SpaceBetween>
      {probe.error ? <ErrorAlert error={probe.error} header="The probe could not run" /> : null}
      {result && (
        <SpaceBetween size="s">
          <ProbeStages result={result} />
          {result.host_key?.state === "unknown" && result.host_key.fingerprint && (
            <Alert type="warning" header="This host's key is not trusted yet"
              action={<Button loading={trust.isPending} onClick={() => setConfirmTrust(true)}>Trust this host</Button>}>
              <SpaceBetween size="xxs">
                <span>Check that this fingerprint matches the host before you trust it. For an EC2 instance, its console output lists the host key fingerprints.</span>
                <Box variant="code">{result.host_key.key_type} {result.host_key.fingerprint}</Box>
              </SpaceBetween>
            </Alert>
          )}
          {result.host_key?.state === "changed" && (
            <Alert type="error" header="The host's key changed">
              The key this host presents does not match the one trusted for it. That can mean the host was rebuilt, or that
              something is intercepting the connection. It cannot be trusted from here: check with the host's owner and fix
              the server's known_hosts file by hand.
            </Alert>
          )}
          {trust.data && !trust.data.trusted && <Alert type="error" header="The key was not trusted">{trust.data.message}</Alert>}
          {trust.error ? <ErrorAlert error={trust.error} header="The key could not be trusted" /> : null}
          {(result.databases?.length ?? 0) > 0 && (
            <Box color="text-body-secondary">The databases found are added to the Databases list. The tool can create its read-only user on this host for you.</Box>
          )}
          {(result.databases?.length ?? 0) === 0 && (result.clusters?.length ?? 0) > 0 && (
            <Box color="text-body-secondary">The instances found are added to the Databases list. Type the database name when you add one.</Box>
          )}
        </SpaceBetween>
      )}
      <Modal visible={confirmTrust} onDismiss={() => setConfirmTrust(false)} header="Trust this host?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirmTrust(false)}>Cancel</Button>
              <Button variant="primary" onClick={doTrust}>I checked it. Trust this host</Button>
            </SpaceBetween>
          </Box>
        }>
        <SpaceBetween size="s">
          <span>The server adds this key to its known_hosts file, then probes again. A different key for this host is never replaced.</span>
          <Box variant="code">{result?.host_key?.key_type} {result?.host_key?.fingerprint}</Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );

  if (variant === "embedded") return body;
  return (
    <Container header={<Header variant="h2" description={description ?? "Probe an SSH host to check that the console can reach it and to find PostgreSQL on it. If the SSH user can run sudo as postgres, the probe also lists the databases, reading only the catalog."}>Look on a database host</Header>}>
      {body}
    </Container>
  );
}
