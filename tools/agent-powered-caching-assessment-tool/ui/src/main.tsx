import "@cloudscape-design/global-styles/index.css";
import { StrictMode, useMemo } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@cloudscape-design/components/i18n";
import messages from "@cloudscape-design/components/i18n/messages/all.en";
import { ApiError } from "./api/errors";
import { ChatProvider } from "./chat/ChatContext";
import { HttpChatTransport } from "./chat/transport";
import { App } from "./App";
import { initTheme } from "./theme";

const mode = initTheme();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A 4xx is an answer, not a blip: do not retry it.
      retry: (count, e) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && count < 2,
      refetchOnWindowFocus: false,
    },
  },
});

function WithChat() {
  // The one place the chat's model transport is chosen. See src/chat/transport.ts.
  const transport = useMemo(() => new HttpChatTransport(), []);
  return (
    <ChatProvider transport={transport}>
      <App initialMode={mode} />
    </ChatProvider>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("No #root element");
createRoot(root).render(
  <StrictMode>
    <I18nProvider locale="en" messages={[messages]}>
      <QueryClientProvider client={queryClient}>
        <WithChat />
      </QueryClientProvider>
    </I18nProvider>
  </StrictMode>,
);
