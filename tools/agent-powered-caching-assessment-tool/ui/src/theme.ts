import { applyMode, applyTheme, disableMotion, Mode, Theme } from "@cloudscape-design/global-styles";

const KEY = "apcat.mode";

export type ColorMode = "light" | "dark";

function systemMode(): ColorMode {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function initialMode(): ColorMode {
  const saved = window.localStorage.getItem(KEY);
  return saved === "dark" || saved === "light" ? saved : systemMode();
}

export function setMode(mode: ColorMode) {
  applyMode(mode === "dark" ? Mode.Dark : Mode.Light);
  window.localStorage.setItem(KEY, mode);
}

/** Visual refresh on, colour mode from the user or the system, motion off when asked. */
export function initTheme(): ColorMode {
  applyTheme(Theme.VisualRefresh);
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  disableMotion(reduce.matches);
  reduce.addEventListener("change", (e) => disableMotion(e.matches));
  const mode = initialMode();
  applyMode(mode === "dark" ? Mode.Dark : Mode.Light);
  return mode;
}
