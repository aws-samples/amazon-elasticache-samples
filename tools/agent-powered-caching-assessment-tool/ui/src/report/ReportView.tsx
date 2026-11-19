import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, SpaceBetween, Spinner, Tabs } from "@cloudscape-design/components";
import { useCapabilities, useOverview, useSummary } from "../api/hooks";
import { ErrorAlert } from "../components/ErrorAlert";
import { QueriesSection, TablesSection } from "./ItemTables";
import { ReportNavProvider, type ReportTab } from "./ReportNav";
import {
  BundlesAndExclusions, GlanceCards, Glossary, KeepInMind, Provenance, ReportFooter,
  RunModeAlerts, ScoringExplainer, SummaryPairs, VerdictBanner, WorkloadSignals,
} from "./Sections";
import { SizingSection } from "./Sizing";

/**
 * The report, drawn from GET /report-data. Every section shows the backend's fields as
 * they are: no rewording, no re-scoring, no re-ordering. The tabs follow the report's
 * own order, verdict first. The verdict banner is the starting point and links onward.
 */
export function ReportView({ jobId }: { jobId: string }) {
  const caps = useCapabilities();
  const ov = useOverview(jobId);
  const summary = useSummary(jobId);
  const [tab, setTab] = useState<ReportTab>("overview");
  const tabsRef = useRef<HTMLDivElement | null>(null);
  const pendingAnchor = useRef<string | null>(null);

  const go = useCallback((next: ReportTab, anchorId?: string) => {
    setTab(next);
    pendingAnchor.current = anchorId ?? null;
    // Scroll after the tab has rendered its content.
    window.requestAnimationFrame(() => {
      const target = (anchorId && document.getElementById(anchorId)) || tabsRef.current;
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      target?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
      if (target instanceof HTMLElement && anchorId) target.focus({ preventScroll: true });
    });
  }, []);

  useEffect(() => { pendingAnchor.current = null; }, [tab]);

  const o = ov.data;
  const nav = useMemo(() => ({ go, scoring: o?.scoring ?? null }), [go, o]);

  if (ov.isLoading || caps.isLoading) {
    return <Box textAlign="center" padding="xl"><Spinner size="large" /> <Box variant="span">Loading the report</Box></Box>;
  }
  if (ov.error) return <ErrorAlert error={ov.error} header="The report could not be loaded" onRetry={() => void ov.refetch()} />;
  if (caps.error) return <ErrorAlert error={caps.error} header="Server capabilities could not be loaded" onRetry={() => void caps.refetch()} />;
  const limits = caps.data?.limits;
  if (!o || !limits) return null;
  const pageSize = limits.report_page_default_limit;
  const c = o.counts ?? {};

  return (
    <ReportNavProvider value={nav}>
      <SpaceBetween size="l">
        <VerdictBanner o={o} />
        <RunModeAlerts o={o} />
        {summary.error ? <ErrorAlert error={summary.error} header="The summary could not be loaded" /> : null}
        <div ref={tabsRef} style={{ scrollMarginBlockStart: "80px" }}>
          <Tabs
            activeTabId={tab}
            onChange={({ detail }) => setTab(detail.activeTabId as ReportTab)}
            ariaLabel="Report sections"
            tabs={[
              { id: "overview", label: "Overview", content: (
                <SpaceBetween size="l">
                  <SummaryPairs s={summary.data} o={o} />
                  <GlanceCards o={o} />
                  <KeepInMind o={o} />
                </SpaceBetween>
              ) },
              { id: "scoring", label: "How scoring works", content: (
                <SpaceBetween size="l"><ScoringExplainer o={o} /><Glossary o={o} /></SpaceBetween>
              ) },
              { id: "workload", label: "Workload", content: <WorkloadSignals o={o} /> },
              { id: "queries", label: `Queries (${c.queries ?? 0})`, content: (
                <QueriesSection jobId={jobId} pageSize={pageSize} lead={o.queries_lead} note={o.queries_note} notCacheable={o.not_cacheable} />
              ) },
              { id: "tables", label: `Tables (${c.tables ?? 0})`, content: <TablesSection jobId={jobId} pageSize={pageSize} /> },
              { id: "other", label: "Bundles & exclusions", content: <BundlesAndExclusions o={o} /> },
              { id: "sizing", label: "Sizing & cost", content: (
                <SizingSection jobId={jobId} sizing={o.sizing} region={caps.data?.region.default ?? ""} />
              ) },
              { id: "provenance", label: "Provenance", content: <Provenance o={o} /> },
            ]}
          />
        </div>
        <ReportFooter o={o} />
      </SpaceBetween>
    </ReportNavProvider>
  );
}
