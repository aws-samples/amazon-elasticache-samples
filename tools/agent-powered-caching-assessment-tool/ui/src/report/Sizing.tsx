import { useMemo, useState } from "react";
import {
  Alert, Box, Button, Checkbox, ColumnLayout, Container, Form, FormField, Header, Input,
  KeyValuePairs, SpaceBetween, Table,
} from "@cloudscape-design/components";
import { useCapabilities, useWhatIf } from "../api/hooks";
import type { CacheSizingOutput, SizedItem, SizingOverrides, WhatIfResult } from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { BulletList, Shown } from "./common";

const num = (v: number | null | undefined, unit = "") =>
  v === null || v === undefined ? null : `${v}${unit}`;

function SizingBody({ s }: { s: CacheSizingOutput }) {
  const n = s.node;
  const c = s.cost;
  const t = s.totals;
  const items: SizedItem[] = [...(s.query_items ?? []), ...(s.object_items ?? [])];
  return (
    <SpaceBetween size="l">
      <KeyValuePairs
        columns={3}
        items={[
          { label: "Status", value: s.status },
          { label: "Status reason", value: <Shown value={s.status_reason} missing="-" /> },
          { label: "Verdict source", value: s.verdict_source },
        ]}
      />
      <ColumnLayout columns={2}>
        <Container header={<Header variant="h3">Node</Header>}>
          {n ? (
            <KeyValuePairs columns={2} items={[
              { label: "Instance type", value: n.instance_type },
              { label: "Nodes", value: `${n.total_nodes} (${n.shards} shard(s), ${n.replicas_per_shard} replica(s) per shard)` },
              { label: "Node memory (GB)", value: String(n.node_memory_gb) },
              { label: "Usable memory, total (GB)", value: String(n.usable_memory_total_gb) },
              { label: "Memory use of usable", value: `${n.memory_utilization_of_usable_pct}%` },
              { label: "Burstable", value: n.burstable ? "Yes" : "No" },
              { label: "Selection basis", value: <Shown value={n.selection_basis} missing="-" /> },
            ]} />
          ) : <Box color="text-body-secondary">No node was chosen.</Box>}
        </Container>
        <Container header={<Header variant="h3" description="On-Demand node list price. An estimate, not a quote.">Cost</Header>}>
          {c ? (
            c.available ? (
              <KeyValuePairs columns={2} items={[
                { label: "Monthly total (USD)", value: <Shown value={num(c.monthly_total)} /> },
                { label: "Monthly per node (USD)", value: <Shown value={num(c.monthly_per_node)} /> },
                { label: "Per node per hour (USD)", value: <Shown value={num(c.price_per_hour_per_node)} /> },
                { label: "Region", value: c.region },
                { label: "Pricing", value: c.pricing_type },
                { label: "Priced at", value: <Shown value={c.priced_at} /> },
              ]} />
            ) : <Box>Cost unavailable: {c.unavailable_reason ?? "no reason recorded"}</Box>
          ) : <Box color="text-body-secondary">Not priced.</Box>}
        </Container>
      </ColumnLayout>
      {t && (
        <KeyValuePairs columns={4} items={[
          { label: "Items sized", value: `${t.items_sized ?? 0} (${t.items_not_sized ?? 0} not sized)` },
          { label: "Memory needed (GB)", value: <Shown value={num(t.memory_needed_gb)} /> },
          { label: "Total ops/s", value: <Shown value={num(t.total_ops_per_sec)} /> },
          { label: "Peak ops/s", value: <Shown value={num(t.peak_ops_per_sec)} /> },
        ]} />
      )}
      <Table<SizedItem>
        variant="embedded"
        header={<Header variant="h3" counter={`(${items.length})`}>Sized items</Header>}
        items={items}
        ariaLabels={{ tableLabel: "Sized items" }}
        wrapLines
        empty={<Box textAlign="center">Nothing was sized.</Box>}
        columnDefinitions={[
          { id: "id", header: "Item", cell: (i) => i.item_id },
          { id: "type", header: "Type", cell: (i) => i.cache_type },
          { id: "sized", header: "Sized", cell: (i) => (i.sized ? "Yes" : <Shown value={i.not_sized_reason} missing="No" />) },
          { id: "key", header: "Key pattern", cell: (i) => <Shown value={i.key_pattern} missing="-" /> },
          { id: "entries", header: "Entries", cell: (i) => <Shown value={num(i.entries)} /> },
          { id: "mem", header: "Memory (bytes)", cell: (i) => <Shown value={num(i.memory_bytes)} /> },
          { id: "hit", header: "Hit rate", cell: (i) => <Shown value={num(i.hit_rate)} /> },
        ]}
      />
      {(s.assumptions ?? []).length > 0 && (
        <div><Box variant="awsui-key-label">Assumptions</Box><BulletList items={s.assumptions ?? []} label="Assumptions" /></div>
      )}
      {(s.warnings ?? []).length > 0 && (
        <Alert type="warning" header="Sizing warnings"><BulletList items={s.warnings ?? []} label="Sizing warnings" /></Alert>
      )}
    </SpaceBetween>
  );
}

function WhatIf({ jobId, defaultRegion }: { jobId: string; defaultRegion: string }) {
  const caps = useCapabilities();
  const run = useWhatIf();
  const [values, setValues] = useState<Record<string, string>>({});
  const [region, setRegion] = useState("");
  const [price, setPrice] = useState(false);
  const params = caps.data?.sizing_parameters ?? [];
  const regionRule = caps.data?.region;

  const errors = useMemo(() => {
    const e: Record<string, string> = {};
    for (const p of params) {
      const raw = values[p.name];
      if (raw === undefined || raw === "") continue;
      const v = Number(raw);
      if (!Number.isFinite(v)) e[p.name] = "Enter a number";
      else if (p.type === "integer" && !Number.isInteger(v)) e[p.name] = "Enter a whole number";
      else if (v < p.minimum || v > p.maximum) e[p.name] = `Between ${p.minimum} and ${p.maximum}`;
    }
    if (region && regionRule && (!new RegExp(regionRule.pattern).test(region) || region.length > regionRule.max_length))
      e.region = regionRule.note;
    return e;
  }, [params, values, region, regionRule]);

  const submit = () => {
    const overrides: Record<string, number> = {};
    for (const p of params) {
      const raw = values[p.name];
      if (raw !== undefined && raw !== "") overrides[p.name] = Number(raw);
    }
    run.mutate({
      jobId,
      body: { parameters: overrides as SizingOverrides, price, ...(region ? { region } : {}) },
    });
  };

  const r: WhatIfResult | undefined = run.data;
  return (
    <Container header={<Header variant="h2" description="Size the recommended cache again with other settings. Nothing is saved and no verdict changes.">What-if sizing</Header>}>
      <SpaceBetween size="l">
        {caps.error ? <ErrorAlert error={caps.error} /> : null}
        <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
          <Form actions={<Button variant="primary" formAction="submit" loading={run.isPending} disabled={Object.keys(errors).length > 0}>Size again</Button>}>
            <SpaceBetween size="m">
              <ColumnLayout columns={3}>
                {params.map((p) => (
                  <FormField key={p.name} label={p.name} description={p.description}
                    constraintText={`${p.minimum} to ${p.maximum}. Default ${p.default}.`} errorText={errors[p.name]}>
                    <Input type="number" inputMode="decimal" value={values[p.name] ?? ""} placeholder={String(p.default)}
                      onChange={({ detail }) => setValues((v) => ({ ...v, [p.name]: detail.value }))} />
                  </FormField>
                ))}
              </ColumnLayout>
              <ColumnLayout columns={2}>
                <FormField label="Region" description="Leave empty to keep the job's region." constraintText={regionRule?.note} errorText={errors.region}>
                  <Input value={region} placeholder={defaultRegion} onChange={({ detail }) => setRegion(detail.value)} />
                </FormField>
                <FormField label="Pricing">
                  <Checkbox checked={price} onChange={({ detail }) => setPrice(detail.checked)}
                    description="Calls the AWS Pricing API with the server's credentials.">
                    Price the scenario
                  </Checkbox>
                </FormField>
              </ColumnLayout>
            </SpaceBetween>
          </Form>
        </form>
        {run.error ? <ErrorAlert error={run.error} header="What-if sizing failed" /> : null}
        {r && (
          <SpaceBetween size="m">
            <KeyValuePairs columns={4} items={[
              { label: "Scenario node", value: <Shown value={r.scenario_summary.instance_type ? `${r.scenario_summary.total_nodes ?? "?"} × ${r.scenario_summary.instance_type}` : null} /> },
              { label: "Scenario memory (GB)", value: String(r.scenario_summary.memory_needed_gb) },
              { label: "Scenario monthly (USD, estimate)", value: <Shown value={r.scenario_summary.cost_available ? num(r.scenario_summary.monthly_total) : null} missing="Not priced" /> },
              { label: "Baseline node", value: <Shown value={r.baseline?.instance_type ? `${r.baseline.total_nodes ?? "?"} × ${r.baseline.instance_type}` : null} missing="No baseline" /> },
            ]} />
            <Table
              variant="embedded"
              header={<Header variant="h3">Changed settings</Header>}
              items={r.changes}
              ariaLabels={{ tableLabel: "Changed settings" }}
              empty={<Box textAlign="center">No setting differs from the job's sizing.</Box>}
              columnDefinitions={[
                { id: "n", header: "Setting", cell: (c) => c.name },
                { id: "b", header: "Job", cell: (c) => <Shown value={c.baseline === null || c.baseline === undefined ? null : String(c.baseline)} /> },
                { id: "s", header: "Scenario", cell: (c) => <Shown value={c.scenario === null || c.scenario === undefined ? null : String(c.scenario)} /> },
              ]}
            />
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}

export function SizingSection({ jobId, sizing, region }: { jobId: string; sizing: CacheSizingOutput | null | undefined; region: string }) {
  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2" description="Counts the entries the statistics window's traffic would create. Not a capacity plan; a pilot sizes it properly.">Sizing &amp; cost</Header>}>
        {sizing ? <SizingBody s={sizing} /> : <Box color="text-body-secondary">Sizing did not run for this assessment.</Box>}
      </Container>
      {sizing && <WhatIf jobId={jobId} defaultRegion={sizing.cost?.region ?? region} />}
    </SpaceBetween>
  );
}
