import { Autosuggest, ColumnLayout, FormField, Input, Toggle } from "@cloudscape-design/components";
import type { Capabilities } from "../api/types";

/** Option values keyed by the option name capabilities gives. */
export type OptionValues = Record<string, boolean | string>;

/** Capabilities' defaults, then any values given (nulls dropped). */
export function defaultOptions(caps: Capabilities, given: Record<string, unknown> = {}): OptionValues {
  const out: OptionValues = Object.fromEntries(caps.options.map((o) => [o.name, o.default]));
  for (const [k, v] of Object.entries(given)) {
    if (typeof v === "boolean" || (typeof v === "string" && v)) out[k] = v;
  }
  return out;
}

export function optionErrors(caps: Capabilities, values: OptionValues, label: string): Record<string, string> {
  const e: Record<string, string> = {};
  for (const o of caps.options) {
    if (o.type !== "string") continue;
    const v = String(values[o.name] ?? "");
    if (o.name === "region") {
      const r = caps.region;
      if (!new RegExp(r.pattern).test(v) || v.length > r.max_length) e[o.name] = r.note;
    }
    if (o.name === "model_id" && values.use_llm === true) {
      const m = caps.model;
      if (!new RegExp(m.pattern).test(v) || v.length > m.max_length) e[o.name] = "Not a model id";
    }
  }
  if (values.require_llm === true && values.use_llm !== true) e.require_llm = "Needs the AI review on";
  if (label.length > caps.limits.label_max_length) e.label = `At most ${caps.limits.label_max_length} characters`;
  if (/[\x00-\x1f\x7f]/.test(label)) e.label = "No control characters";
  return e;
}

const LABELS: Record<string, string> = {
  use_llm: "AI review", model_id: "Model", require_llm: "Require the model",
  size: "Size and cost", price: "Price", region: "Pricing region",
};

/** Every submit option the server publishes, drawn from GET /capabilities. */
export function JobOptionsForm({ caps, values, onChange, label, onLabel, errors, columns = 2 }: {
  caps: Capabilities;
  values: OptionValues;
  onChange: (v: OptionValues) => void;
  label: string;
  onLabel: (v: string) => void;
  errors: Record<string, string>;
  columns?: 1 | 2 | 3;
}) {
  const llmOn = values.use_llm === true;
  const set = (k: string, v: boolean | string) => {
    const next = { ...values, [k]: v };
    if (k === "use_llm" && v === false) next.require_llm = false;
    onChange(next);
  };
  return (
    <ColumnLayout columns={columns}>
      <FormField label="Label" description="A name for the job in the list. Optional."
        constraintText={`At most ${caps.limits.label_max_length} characters.`} errorText={errors.label}>
        <Input value={label} onChange={({ detail }) => onLabel(detail.value)} />
      </FormField>
      {caps.options.map((o) => {
        const title = LABELS[o.name] ?? o.name;
        if (o.name === "model_id") {
          return (
            <FormField key={o.name} label={title} description={o.description} errorText={errors[o.name]}
              constraintText={llmOn ? caps.model.note : "Used only with the AI review."}>
              <Autosuggest
                value={String(values.model_id ?? "")}
                onChange={({ detail }) => set("model_id", detail.value)}
                options={caps.model.suggestions.map((m) => ({ value: m, ...(m === caps.model.default ? { description: "Default" } : {}) }))}
                enteredTextLabel={(v) => `Use: ${v}`}
                placeholder={caps.model.default}
                disabled={!llmOn}
                ariaLabel="Model"
                empty="No suggestions"
              />
            </FormField>
          );
        }
        if (o.type === "boolean") {
          const disabled = o.name === "require_llm" && !llmOn;
          return (
            <FormField key={o.name} label={title} description={o.description} errorText={errors[o.name]}>
              <Toggle checked={Boolean(values[o.name])} disabled={disabled}
                onChange={({ detail }) => set(o.name, detail.checked)}>
                {values[o.name] ? "On" : "Off"}
              </Toggle>
            </FormField>
          );
        }
        return (
          <FormField key={o.name} label={title} description={o.description}
            constraintText={o.name === "region" ? caps.region.note : undefined} errorText={errors[o.name]}>
            <Input value={String(values[o.name] ?? "")} onChange={({ detail }) => set(o.name, detail.value)} />
          </FormField>
        );
      })}
    </ColumnLayout>
  );
}

/** The option values as the submit query and body fields the generated types name. */
export function optionFields(values: OptionValues, label: string) {
  const bool = (k: string) => (typeof values[k] === "boolean" ? (values[k] as boolean) : undefined);
  const str = (k: string) => (typeof values[k] === "string" && values[k] !== "" ? (values[k] as string) : undefined);
  const out: { use_llm?: boolean; size?: boolean; price?: boolean; region?: string; label?: string;
    model_id?: string; require_llm?: boolean } = {};
  const u = bool("use_llm"); if (u !== undefined) out.use_llm = u;
  const sz = bool("size"); if (sz !== undefined) out.size = sz;
  const p = bool("price"); if (p !== undefined) out.price = p;
  const r = str("region"); if (r !== undefined) out.region = r;
  if (u) {
    const m = str("model_id"); if (m !== undefined) out.model_id = m;
    if (bool("require_llm")) out.require_llm = true;
  }
  if (label.trim()) out.label = label.trim();
  return out;
}
