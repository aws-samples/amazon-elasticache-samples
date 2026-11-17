import {
  Box, Button, ColumnLayout, ExpandableSection, KeyValuePairs, SpaceBetween,
} from "@cloudscape-design/components";
import type { MetricSet, NotCacheableGroup, QueryItem, TableItem } from "../api/types";
import { useAgent, type FocusTarget } from "../agent/AgentContext";
import { BulletList, Code, ScoreCell, Shown } from "./common";

// The body of a query or table card, shown when its row is expanded. Every value is the
// report's own field; nothing is reworded or recomputed.

function MetricPairs({ m, idle }: { m: MetricSet; idle?: boolean }) {
  const d = m.display;
  if (idle) return <Box color="text-status-inactive">No traffic was measured, so the report prints no metrics for it.</Box>;
  return (
    <KeyValuePairs
      columns={4}
      items={[
        { label: "Access / Pressure / Efficiency", value: <Shown value={d.access_pressure_efficiency} /> },
        { label: "Penalty", value: m.hard_excluded
          ? <Box variant="span" color="text-status-error">{d.penalty ?? "Hard Exclusion"}</Box>
          : <Shown value={d.penalty} /> },
        { label: "Band", value: <Shown value={d.band} /> },
        { label: "Model confidence", value: <Shown value={d.model_confidence} missing="No model" /> },
        { label: "Score", value: <ScoreCell m={m} /> },
        { label: "DB ms/s", value: <Shown value={d.db_ms_per_sec} />,
          info: m.db_includes_joins ? <Box variant="small">† includes joined queries</Box> : undefined },
      ]}
    />
  );
}

function Factors({ factors }: { factors: ReadonlyArray<{ label: string; text: string }> }) {
  if (!factors.length) return null;
  return <KeyValuePairs columns={1} items={factors.map((f) => ({ label: f.label, value: f.text }))} />;
}

export function AskButtons({ target }: { target: FocusTarget }) {
  const { ask } = useAgent();
  const noun = target.focus.kind;
  return (
    <SpaceBetween direction="horizontal" size="xs">
      <Button iconName="gen-ai" onClick={() => ask(target,
        target.recommended ? `Why is this ${noun} recommended?` : `Why is this ${noun} not recommended?`)}>
        {target.recommended ? "Why recommended?" : "Why not recommended?"}
      </Button>
      <Button variant="normal" onClick={() => ask(target)}>Ask about it</Button>
    </SpaceBetween>
  );
}

function Reasoning({ text, byModel }: { text: string | null | undefined; byModel: boolean }) {
  if (!byModel) {
    return <Box color="text-body-secondary">No model reviewed this item. Its verdict is the score band.</Box>;
  }
  if (!text) return null;
  return (
    <ExpandableSection headerText="Full reasoning" variant="default">
      <Box variant="p">{text}</Box>
    </ExpandableSection>
  );
}

function EvidenceConcerns({ evidence, concerns }: { evidence: string[]; concerns: string[] }) {
  return (
    <ColumnLayout columns={2} variant="text-grid">
      <div>
        <Box variant="awsui-key-label">Evidence</Box>
        <BulletList items={evidence} label="Evidence" />
      </div>
      <div>
        <Box variant="awsui-key-label">Concerns</Box>
        <BulletList items={concerns} label="Concerns" />
      </div>
    </ColumnLayout>
  );
}

export function queryTarget(q: QueryItem): FocusTarget {
  return { focus: { kind: "query", id: q.queryid }, label: `Query ${q.queryid}`, recommended: q.recommended };
}

export function tableTarget(t: TableItem): FocusTarget {
  return { focus: { kind: "table", id: t.table_id }, label: `Table ${t.table_id}`, recommended: t.recommended };
}

export function QueryDetail({ q, notCacheable }: { q: QueryItem; notCacheable: NotCacheableGroup[] }) {
  const group = notCacheable.find((g) => g.items.some((i) => i.queryid === q.queryid));
  const why = group?.items.find((i) => i.queryid === q.queryid)?.why;
  return (
    <SpaceBetween size="l">
      <AskButtons target={queryTarget(q)} />
      {!q.is_candidate && (
        <Box>
          {group
            ? <><Box variant="span" fontWeight="bold">{group.label}:</Box> <Shown value={why} missing="No reason recorded" /></>
            : "Not cacheable. The report records no further reason."}
        </Box>
      )}
      <MetricPairs m={q.metrics} />
      <Factors factors={q.factors ?? []} />
      <EvidenceConcerns evidence={q.evidence ?? []} concerns={q.concerns ?? []} />
      <KeyValuePairs
        columns={3}
        items={[
          { label: "Query id", value: q.queryid },
          { label: "Share of database time", value: <Shown value={q.db_time_share_pct === null || q.db_time_share_pct === undefined ? null : `${q.db_time_share_pct}%`} /> },
          { label: "Suggested cache key", value: <Shown value={q.suggested_cache_key} missing="None" /> },
          { label: "Key columns", value: <Shown value={q.key_columns?.join(", ")} missing="None" /> },
          { label: "Key kind", value: <Shown value={q.key_kind} missing="-" /> },
          { label: "Cached columns", value: <Shown value={q.cached_columns?.join(", ")} missing="None" /> },
          { label: "Invalidated by", value: q.invalidated_by?.length
            ? <BulletList items={q.invalidated_by} label="Invalidated by" /> : <Shown value={null} missing="None recorded" /> },
        ]}
      />
      <Reasoning text={q.reasoning} byModel={q.judged_by_model} />
      <ExpandableSection headerText="SQL" headerDescription="Query text may hold literal values. Treat it as sensitive.">
        <Code>{q.sql}</Code>
      </ExpandableSection>
    </SpaceBetween>
  );
}

export function TableDetail({ t }: { t: TableItem }) {
  return (
    <SpaceBetween size="l">
      <AskButtons target={tableTarget(t)} />
      {t.ruled_out && <Box color="text-status-error">Ruled out before any judgement.</Box>}
      <MetricPairs m={t.metrics} idle={t.idle} />
      <Factors factors={t.factors ?? []} />
      <EvidenceConcerns evidence={t.evidence ?? []} concerns={t.concerns ?? []} />
      <KeyValuePairs
        columns={3}
        items={[
          { label: "Size (MB)", value: <Shown value={t.size_mb === null || t.size_mb === undefined ? null : String(t.size_mb)} /> },
          { label: "Buffer hit ratio", value: <Shown value={t.buffer_hit_ratio_pct === null || t.buffer_hit_ratio_pct === undefined ? null : `${t.buffer_hit_ratio_pct}%`} /> },
          { label: "Cached columns", value: <Shown value={t.cached_columns?.join(", ")} missing="None" /> },
          { label: "Caching strategy", value: <Shown value={t.caching_strategy} missing="None recorded" /> },
          { label: "Invalidation", value: <Shown value={t.invalidation_concern} missing="None recorded" /> },
        ]}
      />
      {(t.bundles ?? []).length > 0 && (
        <div>
          <Box variant="awsui-key-label">Bundles</Box>
          <BulletList
            label="Bundles"
            items={(t.bundles ?? []).map((b) =>
              `Query ${b.queryid}${b.db_time_share_pct === null || b.db_time_share_pct === undefined ? "" : ` (${b.db_time_share_pct}% of DB time)`}: ${b.qualifies ? "qualifies" : "does not qualify"}; this table ${b.this_table_qualifies ? "qualifies" : "does not qualify"}${b.this_table_reason ? ` (${b.this_table_reason})` : ""}${b.other_tables?.length ? `; with ${b.other_tables.join(", ")}` : ""}`)}
          />
        </div>
      )}
      <Reasoning text={t.reasoning} byModel={t.judged_by_model} />
    </SpaceBetween>
  );
}
