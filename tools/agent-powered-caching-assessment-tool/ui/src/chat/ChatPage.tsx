import { useEffect, useRef, useState } from "react";
import {
  Alert, Autosuggest, Badge, Box, Button, ButtonDropdown, FormField, Header, PromptInput, SpaceBetween,
} from "@cloudscape-design/components";
import { Avatar, ChatBubble, SupportPromptGroup } from "@cloudscape-design/chat-components";
import * as tokens from "@cloudscape-design/design-tokens";
import { useCapabilities, useJobStatus, useOverview } from "../api/hooks";
import { formatTime, modelTurnedOff } from "../components/status";
import { navigate } from "../router";
import { useChat } from "./ChatContext";
import { scopeLine } from "./scope";
import { ToolCard, ToolTrace } from "./cards";
import type { Entry } from "./conversation";
import { currentAssessment, followups, type JobFacts } from "./followups";

const AGENT = (loading = false) => (
  <Avatar color="gen-ai" iconName="gen-ai" ariaLabel="Assistant" tooltipText="Assistant" loading={loading} />
);
const YOU = <Avatar iconName="user-profile" ariaLabel="You" tooltipText="You" />;

function Greeting() {
  return (
    <ChatBubble type="incoming" ariaLabel="Assistant greeting" avatar={AGENT()}>
      <SpaceBetween size="xs">
        <Box variant="p">
          Hi. I am the assistant of the Agent Powered Caching Assessment Tool. I can tell you whether a self-managed
          PostgreSQL workload would gain from an Amazon ElastiCache for Valkey cache, which queries
          and tables, and what it would cost.
        </Box>
        <ul style={{ margin: 0, paddingInlineStart: tokens.spaceScaledL }}>
          <li>Start assessments of live databases, several at once, or of collections you saved.</li>
          <li>Follow them as they run, then summarise their reports.</li>
          <li>Explain why any query or table was or was not recommended, citing the report.</li>
          <li>Cancel, rerun or delete an assessment, or size it again with other settings.</li>
          <li>Answer general questions about caching patterns and adding Amazon ElastiCache for Valkey to an application, from the AWS ElastiCache skill.</li>
        </ul>
        <Box variant="small" color="text-body-secondary">
          Anything that changes something is shown to you first, with options you can change, and
          runs only when you confirm. Connection details go in a form and never reach the model.
        </Box>
      </SpaceBetween>
    </ChatBubble>
  );
}

function EntryView({ e }: { e: Entry }) {
  switch (e.kind) {
    case "user":
      return <ChatBubble type="outgoing" ariaLabel="You said" avatar={YOU}>{e.text}</ChatBubble>;
    case "assistant":
      return (
        <ChatBubble type="incoming" ariaLabel="Assistant said" avatar={AGENT(e.streaming)} showLoadingBar={e.streaming}>
          <span style={{ whiteSpace: "pre-wrap" }}>{e.text}</span>
        </ChatBubble>
      );
    case "notice":
      return (
        <Alert type={e.tone} statusIconAriaLabel={e.tone} header={e.code ? <Box variant="code">{e.code}</Box> : undefined}>
          {e.text}{e.requestId ? <Box variant="small">Request ID {e.requestId}</Box> : null}
        </Alert>
      );
    case "tool":
      return (
        <ChatBubble type="incoming" ariaLabel={`Tool ${e.name}`} avatar={AGENT(e.phase === "running" || e.phase === "executing")} hideAvatar>
          <SpaceBetween size="xs">
            <ToolCard e={e} />
            <ToolTrace e={e} hideInput={e.name === "present_answer"} />
          </SpaceBetween>
        </ChatBubble>
      );
  }
}

/** What the API says about the assessment in focus, for the suggestion chips. */
function useJobFacts(jobId: string | null): JobFacts | null {
  const st = useJobStatus(jobId ?? "", !!jobId);
  const hasAnalysis = !!st.data?.available?.includes("analyze");
  const ov = useOverview(jobId ?? "", !!jobId && hasAnalysis && st.data?.status === "complete");
  if (!jobId) return null;
  const s = st.data;
  const meta = ov.data?.meta;
  return {
    jobId, status: s?.status ?? null, available: s?.available ?? [], cancelRequested: !!s?.cancel_requested,
    counts: ov.data?.counts ?? null, hasSizing: !!ov.data?.sizing,
    modelNotUsed: !!meta && meta.interpretation_mode !== "llm" && !modelTurnedOff(meta.fallback_reason),
  };
}

function ModelPicker() {
  const chat = useChat();
  const caps = useCapabilities();
  const [text, setText] = useState(chat.model ?? "");
  const rule = caps.data?.model;
  const bad = !!text && !!rule && !new RegExp(rule.pattern).test(text);
  if (!rule) return null;
  const commit = (v: string) => {
    setText(v);
    if (!v) chat.setModel(null);
    else if (new RegExp(rule.pattern).test(v)) chat.setModel(v);
  };
  return (
    <FormField label="Assistant model" errorText={bad ? "Not a model id" : undefined} stretch={false}>
      <div style={{ minWidth: "22rem" }}>
        <Autosuggest
          value={text}
          onChange={({ detail }) => commit(detail.value)}
          options={rule.suggestions.map((m) => ({ value: m, ...(m === rule.default ? { description: "Default" } : {}) }))}
          enteredTextLabel={(v) => `Use: ${v}`}
          placeholder={`${rule.default} (default)`}
          ariaLabel="Assistant model"
          empty="No suggestions"
        />
      </div>
    </FormField>
  );
}

/** The saved conversations, newest first, to open one. */
function ConversationsMenu({ onOpen }: { onOpen: (id: string) => void }) {
  const chat = useChat();
  const items = chat.conversations.map((c) => ({
    id: c.id, text: c.title, secondaryText: formatTime(c.updatedAt), disabled: c.id === chat.conversationId,
  }));
  return (
    <ButtonDropdown items={items} disabled={!items.length} ariaLabel="Saved conversations"
      onItemClick={({ detail }) => onOpen(detail.id)}>
      Conversations
    </ButtonDropdown>
  );
}

/**
 * The chat workspace. As a page (#/chat) the address names the conversation. As the
 * drawer on every other page it is about what is on screen, and the address is left alone.
 */
export function ChatPage({ conversation, variant = "page" }: { conversation?: string | undefined; variant?: "page" | "drawer" }) {
  const chat = useChat();
  const { state } = chat;
  const [draft, setDraft] = useState("");
  const end = useRef<HTMLDivElement | null>(null);
  const job = chat.contextJob ?? currentAssessment(state);
  const facts = useJobFacts(job);
  const chips = followups(state, facts, chat.focusFacts);
  const thinking = state.busy && state.entries.at(-1)?.kind !== "assistant";
  const page = variant === "page";

  // The address names the conversation: open it, and keep the address in step.
  useEffect(() => {
    if (page && conversation && conversation !== chat.conversationId) chat.openConversation(conversation);
  }, [conversation]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (page && state.entries.length && conversation !== chat.conversationId) {
      navigate({ name: "chat", conversation: chat.conversationId });
    }
  }, [state.entries.length, chat.conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // In the drawer, "nearest" scrolls the drawer only, not the page under it.
    end.current?.scrollIntoView({ block: page ? "end" : "nearest", behavior: reduce ? "auto" : "smooth" });
  }, [state.entries.length, state.entries.at(-1)]);

  const send = (text: string) => { chat.send(text); setDraft(""); };
  const openSaved = (id: string) => (page ? navigate({ name: "chat", conversation: id }) : chat.openConversation(id));
  const newConversation = () => { chat.reset(); if (page) navigate({ name: "chat" }); };

  return (
    <div style={page
      ? { display: "flex", flexDirection: "column", minHeight: "calc(100vh - 120px)" }
      : { display: "flex", flexDirection: "column", minHeight: "100%", paddingInline: tokens.spaceScaledL, paddingBlockStart: tokens.spaceScaledL }}>
      <Header
        variant={page ? "h1" : "h2"}
        description={scopeLine(chat.scope, job, chat.focusFacts?.label ?? null)}
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <ConversationsMenu onOpen={openSaved} />
            <Button iconName="add-plus" onClick={newConversation} disabled={!state.entries.length}
              ariaLabel="New conversation">{page ? "New conversation" : "New"}</Button>
            {!page && (
              <Button iconName="expand" variant="icon" ariaLabel="Open the assistant as a full page"
                href={`#/chat/${chat.conversationId}`} onFollow={(e) => {
                  e.preventDefault();
                  chat.setDrawerOpen(false);
                  navigate(state.entries.length ? { name: "chat", conversation: chat.conversationId } : { name: "chat" });
                }} />
            )}
          </SpaceBetween>
        }
      >
        {page ? "Caching assessment assistant" : "Assistant"}
      </Header>

      <div role="log" aria-live="polite" aria-label="Conversation" style={{ flex: 1, paddingBlock: tokens.spaceScaledL }}>
        <SpaceBetween size="m">
          <Greeting />
          {state.hold && (
            <Alert type="info">This is a saved conversation. Anything it was waiting on was not run. Connection details are not kept, so give them again to start a new assessment.</Alert>
          )}
          {state.entries.map((e) => <EntryView key={e.id} e={e} />)}
          {thinking && (
            <ChatBubble type="incoming" ariaLabel="Assistant is working" avatar={AGENT(true)} showLoadingBar>
              <Box color="text-body-secondary">Working on it</Box>
            </ChatBubble>
          )}
          <div ref={end} />
        </SpaceBetween>
      </div>

      <div style={{ position: "sticky", insetBlockEnd: 0,
        background: page ? tokens.colorBackgroundLayoutMain : tokens.colorBackgroundContainerContent,
        paddingBlock: tokens.spaceScaledS, borderBlockStart: `1px solid ${tokens.colorBorderDividerDefault}` }}>
        <SpaceBetween size="xs">
          {chips.length > 0 && (
            <SupportPromptGroup
              ariaLabel="Suggested next steps"
              alignment="horizontal"
              items={chips.map((c) => ({ id: c.id, text: c.text }))}
              onItemClick={({ detail }) => { const c = chips.find((x) => x.id === detail.id); if (c) send(c.text); }}
            />
          )}
          <PromptInput
            value={draft}
            onChange={({ detail }) => setDraft(detail.value)}
            onAction={({ detail }) => send(detail.value)}
            placeholder="Ask the assistant, for example: assess my database for caching"
            actionButtonIconName="send"
            actionButtonAriaLabel="Send"
            ariaLabel="Message to the assistant"
            disableActionButton={state.busy || !draft.trim()}
            minRows={3}
            maxRows={8}
          />
          <SpaceBetween direction="horizontal" size="m" alignItems="end">
            {page && <ModelPicker />}
            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
              {state.mode === "local"
                ? <Badge color="grey">No model: answers come straight from the report</Badge>
                : <Badge color="blue">{state.modelId ? `Model ${state.modelId}` : "Model on the server"}</Badge>}
              <Box variant="small" color="text-body-secondary">{chat.transport.description}</Box>
            </SpaceBetween>
          </SpaceBetween>
        </SpaceBetween>
      </div>
    </div>
  );
}
