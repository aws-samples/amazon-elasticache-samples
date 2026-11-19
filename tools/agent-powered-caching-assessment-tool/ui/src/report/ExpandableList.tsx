import { useId, useState, type ReactNode } from "react";
import { Box, Icon } from "@cloudscape-design/components";
import * as tokens from "@cloudscape-design/design-tokens";

// A table whose rows open in place. Cloudscape's Table expands rows into child rows of the
// same columns only, so a card cannot open under its row there. This keeps a table's look
// (a header row, one line per item, fixed column widths) and opens the item's full card
// directly below its row.

export interface ListColumn<T> {
  id: string;
  header: string;
  /** A CSS grid track, e.g. "180px" or "minmax(200px, 1fr)". */
  width: string;
  cell: (item: T) => ReactNode;
  align?: "start" | "end";
}

const cellStyle = (align: "start" | "end" | undefined): React.CSSProperties => ({
  minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", textAlign: align ?? "start",
});

export function ExpandableList<T>({ items, columns, getId, rowLabel, renderDetail, loading, empty }: {
  items: ReadonlyArray<T>;
  columns: ReadonlyArray<ListColumn<T>>;
  getId: (item: T) => string;
  rowLabel: (item: T) => string;
  renderDetail: (item: T) => ReactNode;
  loading?: boolean;
  empty: ReactNode;
}) {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const base = useId();
  const grid = `${tokens.spaceScaledXl} ${columns.map((c) => c.width).join(" ")}`;
  const toggle = (id: string) =>
    setOpen((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  const allOpen = items.length > 0 && items.every((i) => open.has(getId(i)));
  const row: React.CSSProperties = {
    display: "grid", gridTemplateColumns: grid, columnGap: tokens.spaceScaledS,
    alignItems: "center", padding: `${tokens.spaceScaledS} ${tokens.spaceScaledM}`,
  };

  return (
    <div role="table" aria-label="Items" aria-busy={loading ? true : undefined} style={{ overflowX: "auto" }}>
      <div style={{ minWidth: "fit-content" }}>
        <div role="row" style={{ ...row, borderBlockEnd: `1px solid ${tokens.colorBorderDividerDefault}` }}>
          <div role="columnheader">
            <button
              type="button"
              onClick={() => setOpen(allOpen ? new Set() : new Set(items.map(getId)))}
              aria-label={allOpen ? "Collapse all rows" : "Expand all rows"}
              title={allOpen ? "Collapse all" : "Expand all"}
              style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: "inherit", display: "flex" }}
            >
              <Icon name={allOpen ? "treeview-collapse" : "treeview-expand"} />
            </button>
          </div>
          {columns.map((c) => (
            <div role="columnheader" key={c.id} style={cellStyle(c.align)}>
              <Box variant="awsui-key-label">{c.header}</Box>
            </div>
          ))}
        </div>

        {items.length === 0 && !loading && <Box textAlign="center" padding="l">{empty}</Box>}

        {items.map((item) => {
          const id = getId(item);
          const isOpen = open.has(id);
          const panel = `${base}-${id}`;
          return (
            <div key={id} style={{ borderBlockEnd: `1px solid ${tokens.colorBorderDividerSecondary}` }}>
              <div
                role="row"
                style={{ ...row, cursor: "pointer", background: isOpen ? tokens.colorBackgroundItemSelected : undefined }}
                onClick={(e) => {
                  // Clicks on a control inside the row (a verdict popover) are not a toggle.
                  if ((e.target as HTMLElement).closest("button, a, [role=dialog]") &&
                      !(e.target as HTMLElement).closest("[data-row-toggle]")) return;
                  toggle(id);
                }}
              >
                <div role="cell">
                  <button
                    type="button"
                    data-row-toggle
                    aria-expanded={isOpen}
                    aria-controls={panel}
                    aria-label={`${isOpen ? "Collapse" : "Expand"} ${rowLabel(item)}`}
                    style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: "inherit", display: "flex" }}
                  >
                    <Icon name={isOpen ? "caret-down-filled" : "caret-right-filled"} />
                  </button>
                </div>
                {columns.map((c) => (
                  <div role="cell" key={c.id} style={cellStyle(c.align)}>{c.cell(item)}</div>
                ))}
              </div>
              {isOpen && (
                <div
                  id={panel}
                  role="region"
                  aria-label={rowLabel(item)}
                  style={{
                    padding: `${tokens.spaceScaledM} ${tokens.spaceScaledL} ${tokens.spaceScaledL}`,
                    paddingInlineStart: `calc(${tokens.spaceScaledXl} + ${tokens.spaceScaledL})`,
                    background: tokens.colorBackgroundContainerContent,
                  }}
                >
                  {renderDetail(item)}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
