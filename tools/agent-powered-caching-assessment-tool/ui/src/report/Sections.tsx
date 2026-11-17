import {
  Alert, Badge, Box, Button, ColumnLayout, Container, ExpandableSection, Header, KeyValuePairs,
  SpaceBetween, Table,
} from "@cloudscape-design/components";
import type { AssessmentSummary, ReportOverview } from "../api/types";
import { modelTurnedOff, toneAlert } from "../components/status";
import { BulletList, Code, Shown } from "./common";
import { useReportNav } from "./ReportNav";

/** The verdict banner: where a reader starts. The text is the report's own, unchanged. */
export function VerdictBanner({ o }: { o: ReportOverview }) {
  const h = o.headline;
  const { go } = useReportNav();
  const c = o.counts ?? {};
  return (
    <Alert type={toneAlert(h.tone)} header={h.title} statusIconAriaLabel={h.tone}>
      <SpaceBetween size="s">
        <Box variant="p" fontSize="heading-s">{h.finding}</Box>
        {h.bottleneck && <Box variant="p"><Box variant="span" fontWeight="bold">Bottleneck:</Box> {h.bottleneck}</Box>}
        {(h.detail || (h.notes ?? []).length > 0) && (
          <ExpandableSection headerText="Read the full summary" variant="footer">
            <SpaceBetween size="xs">
              {h.detail && <Box variant="p">{h.detail}</Box>}
              {(h.notes ?? []).length > 0 && <BulletList items={h.notes ?? []} label="Notes" />}
            </SpaceBetween>
          </ExpandableSection>
        )}
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" iconName="angle-right" iconAlign="right" onClick={() => go("overview", "workload-summary")}>
            Next: workload summary
          </Button>
          <Button onClick={() => go("queries")}>
            See the queries{typeof c.queries === "number" ? ` (${c.queries})` : ""}
          </Button>
          <Button variant="link" onClick={() => go("scoring")}>How scoring works</Button>
        </SpaceBetween>
      </SpaceBetween>
    </Alert>
  );
}

export function RunModeAlerts({ o }: { o: ReportOverview }) {
  const m = o.meta;
  return (
    <>
      {m.assessment_mode === "degraded" && (
        <Alert type="warning" header="Degraded assessment">
          Query-level statistics were not available, so confidence is capped at 60 and the result is not conclusive.
          {m.degraded_reason ? ` ${m.degraded_reason}` : ""}
        </Alert>
      )}
      {m.interpretation_mode !== "llm" && (modelTurnedOff(m.fallback_reason) ? (
        <Alert type="info" header="The AI review was off for this run">
          Every verdict is the deterministic score band.
        </Alert>
      ) : (
        <Alert type="warning" header="The model was requested but not used">
          Every verdict is the deterministic score band, not the AI review.{m.fallback_reason ? ` Reason: ${m.fallback_reason}` : ""}
        </Alert>
      ))}
    </>
  );
}

export function SummaryPairs({ s, o }: { s: AssessmentSummary | undefined; o: ReportOverview }) {
  const sz = s?.sizing;
  const { go } = useReportNav();
  return (
    <div id="workload-summary" tabIndex={-1} style={{ outline: "none" }}>
    <Container header={
      <Header variant="h2" description={o.source.line}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => go("workload")}>Workload signals</Button>
            <Button variant="primary" iconName="angle-right" iconAlign="right" onClick={() => go("queries")}>Next: queries</Button>
          </SpaceBetween>
        }>
        Workload summary
      </Header>
    }>
      <KeyValuePairs
        columns={4}
        items={[
          { label: "Workload verdict", value: <Shown value={s?.verdict ?? o.headline.verdict} missing="None (no model)" /> },
          { label: "Workload archetype", value: <Shown value={s?.workload_archetype ?? o.headline.archetype} /> },
          { label: "Primary bottleneck", value: <Shown value={s?.primary_bottleneck} /> },
          { label: "Cacheable DB time (ms/s)", value: <Shown value={s?.cacheable_db_time_ms_per_sec === null || s?.cacheable_db_time_ms_per_sec === undefined ? null : String(s.cacheable_db_time_ms_per_sec)} /> },
          { label: "Queries recommended", value: <Shown value={s ? String(s.queries_recommended) : null} /> },
          { label: "Query results to cache", value: <Shown value={s ? String(s.query_results_to_cache) : null} /> },
          { label: "Key lookups to cache", value: <Shown value={s ? String(s.key_lookups_to_cache) : null} /> },
          { label: "Tables recommended", value: <Shown value={s ? String(s.tables_recommended) : null} /> },
          { label: "Bundles", value: <Shown value={s ? `${s.bundles} (${s.bundles_that_qualify} qualify)` : null} /> },
          { label: "Sizing", value: sz
            ? <Shown value={`${sz.status}${sz.instance_type ? `: ${sz.total_nodes ?? "?"} × ${sz.instance_type}` : ""}${sz.cost_available && sz.monthly_total !== null && sz.monthly_total !== undefined ? `, about $${sz.monthly_total}/month On-Demand (estimate)` : ""}`} />
            : <Shown value={null} missing="Not sized" /> },
          { label: "Assessment mode", value: o.meta.assessment_mode },
          { label: "Interpretation", value: o.meta.interpretation_mode },
        ]}
      />
    </Container>
    </div>
  );
}

export function GlanceCards({ o }: { o: ReportOverview }) {
  return (
    <Container header={<Header variant="h2">At a glance</Header>}>
      <ColumnLayout columns={Math.min(4, Math.max(1, o.at_a_glance.length))} variant="text-grid">
        {o.at_a_glance.map((g) => (
          <div key={g.id}>
            <Box variant="awsui-key-label">{g.label}</Box>
            <Box variant="awsui-value-large"><Shown value={g.display} /></Box>
            <Box variant="small" color="text-body-secondary">{g.note}</Box>
          </div>
        ))}
      </ColumnLayout>
    </Container>
  );
}

export function KeepInMind({ o }: { o: ReportOverview }) {
  if (!o.keep_in_mind.length) return null;
  return (
    <Container header={<Header variant="h2">Keep in mind</Header>}>
      <BulletList items={o.keep_in_mind} label="Keep in mind" />
    </Container>
  );
}

export function Glossary({ o }: { o: ReportOverview }) {
  if (!o.glossary.length) return null;
  return (
    <Container header={<Header variant="h2">Glossary</Header>}>
      <KeyValuePairs columns={2} items={o.glossary.map((g) => ({ label: g.term, value: g.definition }))} />
    </Container>
  );
}

export function ScoringExplainer({ o }: { o: ReportOverview }) {
  const s = o.scoring;
  return (
    <Container header={<Header variant="h2" description={s.intro}>How scoring works</Header>}>
      <SpaceBetween size="l">
        <Table
          variant="embedded"
          items={s.pillars}
          ariaLabels={{ tableLabel: "Scoring pillars" }}
          wrapLines
          columnDefinitions={[
            { id: "name", header: "Pillar", cell: (p) => p.name },
            { id: "weight", header: "Weight", cell: (p) => p.weight },
            { id: "q", header: "Question", cell: (p) => p.question },
            { id: "d", header: "Drivers", cell: (p) => p.drivers },
          ]}
        />
        <Box variant="p">{s.formula}</Box>
        {s.hit_rate_note && <Box variant="p">{s.hit_rate_note}</Box>}
        <Table
          variant="embedded"
          items={s.bands}
          ariaLabels={{ tableLabel: "Score bands" }}
          columnDefinitions={[
            { id: "label", header: "Band", cell: (b) => b.label },
            { id: "range", header: "Score", cell: (b) => `${b.min_score} to ${b.max_score}` },
            { id: "rec", header: "Recommended", cell: (b) => (b.recommended ? "Yes" : "No") },
          ]}
        />
        {s.bands_note && <Box variant="p">{s.bands_note}</Box>}
        {s.judge_note && <Box variant="p">{s.judge_note}</Box>}
      </SpaceBetween>
    </Container>
  );
}

export function WorkloadSignals({ o }: { o: ReportOverview }) {
  return (
    <Table
      variant="container"
      header={<Header variant="h2">Workload signals</Header>}
      items={o.workload}
      ariaLabels={{ tableLabel: "Workload signals" }}
      wrapLines
      empty={<Box textAlign="center">No workload signals were recorded.</Box>}
      columnDefinitions={[
        { id: "label", header: "Signal", cell: (w) => w.label },
        { id: "value", header: "Value", cell: (w) => <Shown value={w.display} /> },
        { id: "note", header: "Note", cell: (w) => w.note || "" },
      ]}
    />
  );
}

export function BundlesAndExclusions({ o }: { o: ReportOverview }) {
  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2" counter={`(${o.bundles.length})`}
        description="Not cacheable queries the application could compute from cached rows.">Bundles</Header>}>
        {o.bundles.length === 0 ? <Box color="text-body-secondary">No bundles in this report.</Box> : (
          <SpaceBetween size="m">
            {o.bundles.map((b) => (
              <ExpandableSection key={b.queryid} variant="container"
                headerText={`Query ${b.queryid}`}
                headerDescription={b.db_time_share_pct === null || b.db_time_share_pct === undefined ? undefined : `${b.db_time_share_pct}% of database time`}
                headerActions={<Badge color={b.qualifies ? "green" : "grey"}>{b.qualifies ? "Qualifies" : "Does not qualify"}</Badge>}>
                <SpaceBetween size="s">
                  <Code>{b.sql}</Code>
                  {b.not_cacheable_reason && <Box>{b.not_cacheable_reason}</Box>}
                  <Table
                    variant="embedded"
                    items={b.table_checks ?? []}
                    ariaLabels={{ tableLabel: `Tables in bundle ${b.queryid}` }}
                    wrapLines
                    columnDefinitions={[
                      { id: "t", header: "Table", cell: (c) => c.table_id },
                      { id: "q", header: "Can be object cached", cell: (c) => (c.qualifies ? "Yes" : "No") },
                      { id: "r", header: "Reason", cell: (c) => <Shown value={c.reason} missing="-" /> },
                      { id: "h", header: "Row change interval (h)", cell: (c) => <Shown value={c.row_change_interval_hours === null || c.row_change_interval_hours === undefined ? null : String(c.row_change_interval_hours)} /> },
                    ]}
                  />
                </SpaceBetween>
              </ExpandableSection>
            ))}
          </SpaceBetween>
        )}
      </Container>

      <Container header={<Header variant="h2" counter={`(${o.not_cacheable.reduce((n, g) => n + g.count, 0)})`}>Not cacheable</Header>}>
        {o.not_cacheable.length === 0 ? <Box color="text-body-secondary">No query was ruled out by the parser.</Box> : (
          <SpaceBetween size="s">
            {o.not_cacheable.map((g) => (
              <ExpandableSection key={g.label} headerText={g.label} headerCounter={`(${g.count})`}>
                <Table
                  variant="embedded"
                  items={g.items}
                  ariaLabels={{ tableLabel: g.label }}
                  wrapLines
                  columnDefinitions={[
                    { id: "sql", header: "Query", cell: (i) => <Box variant="code" fontSize="body-s">{i.sql_short}</Box> },
                    { id: "cps", header: "Calls/s", cell: (i) => <Shown value={i.calls_per_second === null || i.calls_per_second === undefined ? null : String(i.calls_per_second)} /> },
                    { id: "why", header: "Why", cell: (i) => <Shown value={i.why} missing="-" /> },
                  ]}
                />
              </ExpandableSection>
            ))}
          </SpaceBetween>
        )}
      </Container>

      <Table
        variant="container"
        header={<Header variant="h2" counter={`(${o.ruled_out_tables.length})`} description="Excluded before any judgement.">Ruled-out tables</Header>}
        items={o.ruled_out_tables}
        ariaLabels={{ tableLabel: "Ruled-out tables" }}
        wrapLines
        empty={<Box textAlign="center">No table was ruled out.</Box>}
        columnDefinitions={[
          { id: "t", header: "Table", cell: (r) => r.table_id },
          { id: "r", header: "Reason", cell: (r) => <Shown value={r.reason} missing="-" /> },
        ]}
      />
    </SpaceBetween>
  );
}

export function Provenance({ o }: { o: ReportOverview }) {
  const p = o.provenance;
  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Judgement</Header>}>
        {p.model_rows
          ? <KeyValuePairs columns={2} items={p.model_rows.map((r) => ({ label: r.label, value: <Shown value={r.value} /> }))} />
          : <Box>{p.no_model_reason ?? "No model was consulted."}</Box>}
      </Container>
      <Container header={<Header variant="h2">Collection</Header>}>
        <SpaceBetween size="m">
          <KeyValuePairs columns={2} items={(p.collection_rows ?? []).map((r) => ({ label: r.label, value: <Shown value={r.value} /> }))} />
          {(p.slowest_stages ?? []).length > 0 && (
            <Table
              variant="embedded"
              header={<Header variant="h3">Slowest stages</Header>}
              items={p.slowest_stages ?? []}
              ariaLabels={{ tableLabel: "Slowest collection stages" }}
              columnDefinitions={[
                { id: "s", header: "Stage", cell: (s) => s.stage },
                { id: "t", header: "Seconds", cell: (s) => <Shown value={s.seconds === null || s.seconds === undefined ? null : String(s.seconds)} /> },
              ]}
            />
          )}
          {(p.collection_warnings ?? []).length > 0 && (
            <Alert type="warning" header="Collection warnings">
              <BulletList items={p.collection_warnings ?? []} label="Collection warnings" />
            </Alert>
          )}
          {p.local_executor_note && <Alert type="info">{p.local_executor_note}</Alert>}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}

export function ReportFooter({ o }: { o: ReportOverview }) {
  return (
    <Box variant="small" color="text-body-secondary">
      <SpaceBetween size="xxs">
        <span>{o.footer.mode_note}</span>
        <span>{o.footer.sensitivity_note}</span>
        <span>
          Job {o.meta.job_id} · contract {o.meta.contract_version} · view model {o.meta.view_model_version} · generated {o.meta.generated_at}
        </span>
      </SpaceBetween>
    </Box>
  );
}
