import { Box, Link, Popover, SpaceBetween, StatusIndicator } from "@cloudscape-design/components";
import type { MetricSet, ScoringSection } from "../api/types";
import { verdictStatus } from "../components/status";
import { useReportNav } from "./ReportNav";

/** A value as the report prints it. Missing is said in words, never shown as 0. */
export function Shown({ value, missing = "Not available" }: { value: string | null | undefined; missing?: string }) {
  if (value === null || value === undefined || value === "") {
    return <Box variant="span" color="text-status-inactive">{missing}</Box>;
  }
  return <>{value}</>;
}

/** The band in the report's scoring section that holds this score, if it has one. */
function bandFor(scoring: ScoringSection | null, m: MetricSet | undefined) {
  if (!scoring || !m || m.score === null || m.score === undefined) return null;
  const s = m.score;
  return scoring.bands.find((b) => s >= b.min_score && s <= b.max_score) ?? null;
}

/** A short explanation of a verdict, from this report's own bands and this item's scores. */
function VerdictHelp({ label, m, byModel }: { label: string; m?: MetricSet; byModel?: boolean }) {
  const { scoring, go } = useReportNav();
  const band = bandFor(scoring, m);
  return (
    <SpaceBetween size="xs">
      {m?.hard_excluded ? (
        <Box>
          This item was ruled out by a hard exclusion, so it has no score. A hard exclusion
          cannot be overturned by a model verdict.
        </Box>
      ) : m?.not_scored ? (
        <Box>This query was stopped by a parser gate before scoring, so it has no score.</Box>
      ) : m && m.score !== null && m.score !== undefined ? (
        <>
          <Box>
            Scored <Box variant="span" fontWeight="bold">{m.display.score ?? String(m.score)}</Box> against
            the default thresholds
            {m.display.access_pressure_efficiency ? <> (Access / Pressure / Efficiency {m.display.access_pressure_efficiency}{m.display.penalty && m.display.penalty !== "0" ? `, penalty ${m.display.penalty}` : ""})</> : null}.
          </Box>
          {band && (
            <Box>
              {band.label} covers scores {band.min_score} to {band.max_score} and is
              {band.recommended ? " a recommended band." : " not a recommended band."}
            </Box>
          )}
        </>
      ) : null}
      {byModel === true && (
        <Box>
          The verdict shown, {label}, is the AI model&apos;s judgement over these measurements, so it
          can differ from the score band.
        </Box>
      )}
      {byModel === false && <Box>No model judged this item, so the verdict is the score band.</Box>}
      {scoring?.bands_note && <Box variant="small" color="text-body-secondary">{scoring.bands_note}</Box>}
      <Link onFollow={(e) => { e.preventDefault(); go("scoring"); }} href="#how-scoring-works">
        How scoring works
      </Link>
    </SpaceBetween>
  );
}

/**
 * A verdict label. Click it for what the verdict means here, with a link to How scoring
 * works. `m` and `byModel` describe the item; without them it explains the bands only.
 */
export function Verdict({ verdict, label, m, byModel }: {
  verdict: string | null | undefined;
  label: string | null | undefined;
  m?: MetricSet;
  byModel?: boolean;
}) {
  if (!verdict && !label) return <Box variant="span" color="text-status-inactive">No verdict</Box>;
  const text = label ?? verdict ?? "";
  return (
    <Popover
      header={text}
      size="medium"
      position="top"
      triggerType="custom"
      dismissAriaLabel="Close"
      renderWithPortal
      content={<VerdictHelp label={text} m={m} byModel={byModel} />}
    >
      <button
        type="button"
        aria-label={`${text}: what this verdict means`}
        style={{
          background: "none", border: "none", padding: 0, margin: 0, cursor: "pointer",
          font: "inherit", color: "inherit", textAlign: "start", whiteSpace: "nowrap",
        }}
      >
        <StatusIndicator type={verdictStatus(verdict)}>
          <span style={{ textDecoration: "underline dotted", textUnderlineOffset: "3px" }}>{text}</span>
        </StatusIndicator>
      </button>
    </Popover>
  );
}

/** The Score cell: a hard exclusion in red, a parser gate as Not scored. */
export function ScoreCell({ m }: { m: MetricSet }) {
  if (m.hard_excluded) return <Box variant="span" color="text-status-error" fontWeight="bold">{m.display.score ?? "Hard Exclusion"}</Box>;
  if (m.not_scored) return <Box variant="span" color="text-status-inactive">{m.display.score ?? "Not scored"}</Box>;
  return <Shown value={m.display.score} missing="Not scored" />;
}

export function BulletList({ items, label }: { items: ReadonlyArray<string>; label: string }) {
  if (!items.length) return <Box color="text-status-inactive">None recorded</Box>;
  return (
    <ul aria-label={label} style={{ margin: 0, paddingInlineStart: "1.25em" }}>
      {items.map((t, i) => <li key={i}>{t}</li>)}
    </ul>
  );
}

export function Code({ children }: { children: string }) {
  return (
    <Box variant="code" display="block">
      <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{children}</span>
    </Box>
  );
}
