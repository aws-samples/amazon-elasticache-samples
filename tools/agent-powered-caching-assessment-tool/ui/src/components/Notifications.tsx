import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { Flashbar, type FlashbarProps } from "@cloudscape-design/components";

type Notify = (item: Omit<FlashbarProps.MessageDefinition, "id" | "onDismiss" | "dismissible">) => void;

const Ctx = createContext<Notify>(() => undefined);

export function NotificationsProvider({ children, render }: {
  children: ReactNode;
  render: (bar: ReactNode) => ReactNode;
}) {
  const [items, setItems] = useState<FlashbarProps.MessageDefinition[]>([]);
  const notify = useCallback<Notify>((item) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setItems((prev) => [
      ...prev.slice(-4),
      { ...item, id, dismissible: true, dismissLabel: "Dismiss",
        onDismiss: () => setItems((p) => p.filter((x) => x.id !== id)) },
    ]);
  }, []);
  const bar = useMemo(() => <Flashbar items={items} stackItems={items.length > 2} />, [items]);
  return <Ctx.Provider value={notify}>{render(bar)}{children}</Ctx.Provider>;
}

export function useNotify(): Notify {
  return useContext(Ctx);
}
