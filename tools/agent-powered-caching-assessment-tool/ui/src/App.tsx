import { useEffect, useState } from "react";
import {
  AppLayout, Badge, Box, BreadcrumbGroup, SideNavigation, Spinner, TopNavigation,
  type BreadcrumbGroupProps, type SideNavigationProps,
} from "@cloudscape-design/components";
import { currentIdentity } from "./api/auth";
import { useDatabase, useRunningCount } from "./api/hooks";
import { useChat } from "./chat/ChatContext";
import { ChatPage } from "./chat/ChatPage";
import { NotificationsProvider } from "./components/Notifications";
import { landing } from "./databases/connection";
import { AssessmentPage } from "./pages/AssessmentPage";
import { ConnectWizard } from "./pages/ConnectWizard";
import { DatabasePage } from "./pages/DatabasePage";
import { DatabasesPage } from "./pages/DatabasesPage";
import { HistoryPage } from "./pages/HistoryPage";
import { NewAssessmentPage } from "./pages/NewAssessmentPage";
import { StartPage } from "./pages/StartPage";
import { follow, href, navigate, useRoute, type Route } from "./router";
import { setMode, type ColorMode } from "./theme";


/** Routes at the top of the navigation. They show no breadcrumbs, only their page title. */
const TOP_LEVEL: ReadonlySet<Route["name"]> = new Set(["home", "start", "databases", "history", "chat"]);

/**
 * The breadcrumbs for a route.
 * @param route The route on screen.
 * @param dbLabel The label of the database it is under, once loaded.
 * @returns The trail, starting at the section the page belongs to.
 */
function crumbs(route: Route, dbLabel: string | null): BreadcrumbGroupProps.Item[] {
  const databases = { text: "Databases", href: href({ name: "databases" }) };
  const assessments = { text: "Assessments", href: href({ name: "history" }) };
  const dbCrumb = (id: string) => ({ text: dbLabel ?? id, href: href({ name: "database", id }) });
  switch (route.name) {
    case "database": return [databases, dbCrumb(route.id)];
    case "connect":
      return route.databaseId
        ? [databases, dbCrumb(route.databaseId), { text: "Edit", href: href(route) }]
        : [databases, { text: "Add a database", href: href(route) }];
    case "assessment":
      return route.databaseId
        ? [databases, dbCrumb(route.databaseId), { text: route.id, href: href(route) }]
        : [assessments, { text: route.id, href: href(route) }];
    case "new": return [assessments, { text: "New assessment", href: href(route) }];
    // Top-level pages have no trail: a single crumb would only repeat the page title.
    case "home": case "start": case "databases": case "history": case "chat": return [];
    default: return [assessments];
  }
}

/** The side navigation entry highlighted for a route. */
function activeHref(route: Route): string {
  if (route.name === "assessment" && route.databaseId) return href({ name: "database", id: route.databaseId });
  if (route.name === "connect") return route.databaseId ? href({ name: "database", id: route.databaseId }) : href({ name: "databases" });
  if (route.name === "new" || route.name === "assessment") return href({ name: "history" });
  if (route.name === "chat") return href({ name: "chat" });
  return href(route);
}

/**
 * The side navigation: Get started, then Databases, Assessments with the running count, and
 * the assistant. Saved databases are listed on the Databases page, not here.
 */
function navItems(running: number): SideNavigationProps.Item[] {
  return [
    { type: "link", text: "Get started", href: href({ name: "start" }) },
    { type: "divider" },
    { type: "link", text: "Databases", href: href({ name: "databases" }) },
    { type: "link", text: "Assessments", href: href({ name: "history" }),
      ...(running > 0 ? { info: <Badge color="blue">{running} running</Badge> } : {}) },
    { type: "link", text: "Assistant", href: href({ name: "chat" }) },
  ];
}

/** #/: Get started. */
function Landing() {
  useEffect(() => { navigate({ name: landing() }, { replace: true }); }, []);
  return <Box textAlign="center" padding="xl"><Spinner size="large" /></Box>;
}

/** The database a route is under, if any. */
function routeDatabase(route: Route): string | undefined {
  if (route.name === "database") return route.id;
  if (route.name === "assessment" || route.name === "connect") return route.databaseId;
  return undefined;
}

export function App({ initialMode }: { initialMode: ColorMode }) {
  const route = useRoute();
  const [mode, setModeState] = useState<ColorMode>(initialMode);
  const [navOpen, setNavOpen] = useState(true);
  const identity = currentIdentity();
  const chat = useChat();
  const { setScope } = chat;
  const running = useRunningCount();
  const dbId = routeDatabase(route);
  const db = useDatabase(dbId ?? "", !!dbId);
  const dbLabel = db.data?.label ?? null;
  const latestDone = db.data?.latest_complete?.job_id ?? null;
  const onChat = route.name === "chat";
  const jobId = route.name === "assessment" ? route.id : null;

  // The assistant drawer is about what is on screen. The full chat page keeps whatever
  // the conversation was about, so it does not change the scope.
  useEffect(() => {
    if (onChat) return;
    if (jobId) setScope({ kind: "assessment", assessmentId: jobId, databaseId: dbId ?? null, label: dbLabel });
    else if (route.name === "database" && dbId) {
      setScope(dbLabel ? { kind: "database", databaseId: dbId, label: dbLabel, assessmentId: latestDone } : null);
    } else setScope(null);
  }, [onChat, jobId, route.name, dbId, dbLabel, latestDone, setScope]);

  const toggleMode = () => {
    const next = mode === "dark" ? "light" : "dark";
    setMode(next);
    setModeState(next);
  };

  const content = (() => {
    switch (route.name) {
      case "home": return <Landing />;
      case "start": return <StartPage />;
      case "databases": return <DatabasesPage />;
      case "database": return <DatabasePage key={route.id} databaseId={route.id} />;
      case "connect": return <ConnectWizard key={`${route.databaseId ?? ""}:${route.pick ?? ""}`} pick={route.pick} databaseId={route.databaseId} />;
      case "assessment": return <AssessmentPage key={route.id} jobId={route.id} />;
      case "new": return <NewAssessmentPage />;
      case "chat": return <ChatPage conversation={route.conversation} />;
      default: return <HistoryPage />;
    }
  })();

  return (
    <NotificationsProvider render={(bar) => (
      <>
        <div id="top-nav" style={{ position: "sticky", top: 0, zIndex: 1002 }}>
          <TopNavigation
            identity={{ href: href({ name: "home" }), title: "Agent Powered Caching Assessment Tool",
              logo: { src: "/apcat-logo.svg", alt: "Agent Powered Caching Assessment Tool" }, onFollow: (e) => follow(e) }}
            utilities={[
              // On the full chat page the assistant is the page, so there is no drawer to open.
              ...(onChat ? [] : [{ type: "button" as const, iconName: "gen-ai" as const, text: "Assistant",
                ariaLabel: chat.drawerOpen ? "Close the assistant" : "Open the assistant",
                onClick: () => chat.setDrawerOpen(!chat.drawerOpen) }]),
              { type: "button", iconName: mode === "dark" ? "star-filled" : "star",
                text: mode === "dark" ? "Light mode" : "Dark mode", ariaLabel: "Switch colour mode", onClick: toggleMode },
              // IDENTITY SEAM: shown only once sign-in exists and names a user. See src/api/auth.ts.
              ...(identity.displayName ? [{ type: "button" as const, iconName: "user-profile" as const,
                text: identity.displayName, ariaLabel: `Account: ${identity.displayName}`, disableTextCollapse: true }] : []),
            ]}
            i18nStrings={{ overflowMenuTriggerText: "More", overflowMenuTitleText: "All" }}
          />
        </div>
        <AppLayout
          headerSelector="#top-nav"
          navigationOpen={navOpen}
          onNavigationChange={({ detail }) => setNavOpen(detail.open)}
          navigation={
            <SideNavigation
              header={{ href: href({ name: "home" }), text: "Caching assessment" }}
              activeHref={activeHref(route)}
              onFollow={(e) => follow(e)}
              items={navItems(running.data ?? 0)}
            />
          }
          breadcrumbs={TOP_LEVEL.has(route.name) ? undefined
            : <BreadcrumbGroup items={crumbs(route, dbLabel)} ariaLabel="Breadcrumbs" onFollow={(e) => follow(e)} />}
          notifications={bar}
          tools={<ChatPage variant="drawer" />}
          toolsHide={onChat}
          toolsOpen={!onChat && chat.drawerOpen}
          onToolsChange={({ detail }) => chat.setDrawerOpen(detail.open)}
          toolsWidth={480}
          maxContentWidth={onChat ? 1280 : undefined}
          ariaLabels={{
            navigation: "Side navigation", navigationClose: "Close navigation", navigationToggle: "Open navigation",
            notifications: "Notifications", tools: "Assistant", toolsClose: "Close the assistant", toolsToggle: "Open the assistant",
          }}
          content={content}
        />
      </>
    )}>
      {null}
    </NotificationsProvider>
  );
}
