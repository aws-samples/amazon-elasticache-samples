import { useEffect, useMemo, useState } from "react";
import {
  Alert, Box, Button, ColumnLayout, Container, ExpandableSection, FormField, Header, Input,
  KeyValuePairs, Link, Modal, RadioGroup, SegmentedControl, SpaceBetween, Spinner, StatusIndicator, Table,
  Tiles, Toggle, Wizard,
} from "@cloudscape-design/components";
import {
  useCapabilities, useCheckDatabase, useCreateDatabase, useDatabase, useDeleteDatabase,
  useCreateRole, useDiscovery, useUpdateDatabase,
} from "../api/hooks";
import { ApiError } from "../api/errors";
import type {
  Capabilities, DatabaseCheckResponse, DatabaseView, DiscoveredServer, DiscoverySource, RoleCreateResult,
} from "../api/types";
import { ErrorAlert } from "../components/ErrorAlert";
import { useNotify } from "../components/Notifications";
import { readinessLabel, readinessStatus } from "../components/status";
import {
  connectionErrors, connectionKey, DEFAULT_ROLE, discoveryNote, EMPTY_CONNECTION, fieldsFromConnection,
  fieldsFromDiscovery, initialUserMode, pssLabel, roleFor, roleRequest, signInErrors, sourceLabel, splitFinds,
  toConnection, type ConnectionFields, type UserMode,
} from "../databases/connection";
import { probeFinds } from "../databases/hostProbe";
import { HostProbePanel } from "../databases/HostProbePanel";
import { follow, href, navigate } from "../router";
import { Checklist } from "./NewAssessmentPage";

type StepId = "choose" | "signin" | "check" | "save";

const READINESS_TEXT: Record<string, string> = {
  ready: "Every check passed. The database is ready for an assessment.",
  warnings: "You can run an assessment. Read the warnings first.",
  degraded: "pg_stat_statements is not usable, so an assessment would run in degraded mode and see less of the workload. You can still run one, or fix it and check again.",
  blocked: "A check failed, so an assessment cannot run yet. Fix what the list says and check again. The connection is saved, so you can also finish now and check later.",
};

/**
 * The saved database id an already-saved conflict names, to offer a link to it.
 * @param e The create or update error.
 * @returns The id in the detail, or null.
 */
function existingId(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.code !== "database_exists") return null;
  return /db-[0-9a-f]{12}/.exec(e.detail)?.[0] ?? null;
}

/** The discovery finds to pick from, one at a time. */
function FoundTable({ items, selected, onSelect, loading, label, empty = "No databases or instances found" }: {
  items: DiscoveredServer[];
  selected: DiscoveredServer | null;
  onSelect: (s: DiscoveredServer) => void;
  loading: boolean;
  label: string;
  empty?: string;
}) {
  return (
    <Table<DiscoveredServer>
      variant="embedded"
      items={items}
      loading={loading}
      loadingText="Looking"
      trackBy="id"
      selectionType="single"
      selectedItems={selected ? [selected] : []}
      onSelectionChange={({ detail }) => { const s = detail.selectedItems[0]; if (s) onSelect(s); }}
      ariaLabels={{ selectionGroupLabel: label, itemSelectionLabel: (_s, i) => `Select ${i.label}`, tableLabel: label }}
      wrapLines
      empty={<Box textAlign="center" color="text-body-secondary">{empty}</Box>}
      columnDefinitions={[
        { id: "label", header: "Database or instance", cell: (s) => s.label },
        { id: "source", header: "Found by", width: 150, cell: (s) => sourceLabel(s.source) },
        { id: "port", header: "Port", width: 80, cell: (s) => s.port ?? "-" },
        { id: "pss", header: "pg_stat_statements", width: 170, cell: (s) => pssLabel(s.pg_stat_statements) },
      ]}
    />
  );
}

/** What a probe or a find said about sudo as postgres on the host. */
type Access = "sudo" | "no_sudo" | "failed" | "not_checked" | null;

/**
 * The sign-in fields: SSH, the database, the database user and the password source, with
 * the host probe. The user is created by the tool (apcat_reader or another name) or one the
 * user has.
 */
function SignInForm({ caps, f, setF, shown, note, creating, attachEarlier, setAttachEarlier, earlier,
  userMode, setUserMode, customRole, setCustomRole, access, setAccess, roleDone, roleFail }: {
  caps: Capabilities;
  f: ConnectionFields;
  setF: (u: (x: ConnectionFields) => ConnectionFields) => void;
  shown: Record<string, string>;
  note: string | null;
  creating: boolean;
  attachEarlier: boolean;
  setAttachEarlier: (v: boolean) => void;
  earlier: boolean;
  userMode: UserMode;
  setUserMode: (m: UserMode) => void;
  customRole: string;
  setCustomRole: (v: string) => void;
  access: Access;
  setAccess: (a: Access) => void;
  roleDone: RoleCreateResult | null;
  roleFail: RoleCreateResult | null;
}) {
  const [probeFound, setProbeFound] = useState<DiscoveredServer[]>([]);
  const [probePick, setProbePick] = useState<DiscoveredServer | null>(null);
  // Open the database address only when it is not the usual 127.0.0.1:5432.
  const [advanced, setAdvanced] = useState(() =>
    (f.cluster_endpoint.trim() || "127.0.0.1") !== "127.0.0.1" || (String(f.port).trim() || "5432") !== "5432");
  const noSudo = access === "no_sudo";
  const createMode = userMode !== "existing";
  const role = roleFor(userMode, customRole) || DEFAULT_ROLE;

  const field = (k: keyof ConnectionFields, label: string, constraint?: string, type: "text" | "number" = "text") => (
    <FormField label={label} constraintText={constraint} errorText={shown[k]}>
      <Input value={String(f[k])} type={type} onChange={({ detail }) => setF((x) => ({ ...x, [k]: detail.value }))} />
    </FormField>
  );

  const takeFound = (s: DiscoveredServer) => {
    setProbePick(s);
    setF((x) => ({
      ...x, ...(s.host ? { cluster_endpoint: s.host } : {}), ...(s.port ? { port: String(s.port) } : {}),
      ...(s.database ? { database_name: s.database } : {}),
    }));
  };

  return (
    <SpaceBetween size="l">
      {note && <Alert type="info">{note}</Alert>}
      <Container header={<Header variant="h2" description="How the tool reaches the database host. The host key must already be in the server's known_hosts file.">SSH</Header>}>
        <SpaceBetween size="l">
          <ColumnLayout columns={2}>
            {field("ssh_target", "SSH target", "user@host, a host, or a name from the server's ssh_config.")}
            {field("ssh_port", "SSH port", undefined, "number")}
            {field("identity_file", "Key name", "Optional. A key file name in the server's key folder (SSH_KEY_DIR).")}
          </ColumnLayout>
          <ExpandableSection headerText="Probe this host" variant="footer"
            headerDescription="Checks that the console can reach the host with this key and finds PostgreSQL on it. When the SSH user can run commands as postgres (sudo), it lists the databases too.">
            <SpaceBetween size="s">
              <HostProbePanel variant="embedded"
                fixed={{ ssh_target: f.ssh_target, ssh_port: String(f.ssh_port || "22"), identity_file: f.identity_file.trim() }}
                onResult={(r) => {
                  setProbePick(null);
                  setProbeFound(probeFinds(r));
                  const a = r.postgres_access ?? null;
                  setAccess(a);
                  if (a === "no_sudo" && userMode !== "existing") setUserMode("existing");
                }} />
              {probeFound.length > 0 && (
                <FoundTable items={probeFound} selected={probePick} onSelect={takeFound}
                  loading={false} label="Found on this host" />
              )}
            </SpaceBetween>
          </ExpandableSection>
        </SpaceBetween>
      </Container>
      <Container header={<Header variant="h2" description="The database the tool assesses on the SSH host.">Database</Header>}>
        <SpaceBetween size="l">
          <ColumnLayout columns={2}>
            {field("database_name", "Database name")}
            {field("schemas", "Schemas", "Optional. Comma separated. Leave empty for all.")}
          </ColumnLayout>
          <ExpandableSection variant="footer" headerText="Advanced: database address on the SSH host"
            expanded={advanced || !!shown.cluster_endpoint || !!shown.port}
            onChange={({ detail }) => setAdvanced(detail.expanded)}
            headerDescription={`${f.cluster_endpoint || "127.0.0.1"}, port ${f.port || "5432"}. Change only if PostgreSQL listens elsewhere on that host.`}>
            <SpaceBetween size="m">
              <Box color="text-body-secondary">
                The tool runs psql on the SSH host, so this is the database's address as seen from that
                host. Leave it as 127.0.0.1 and 5432 unless PostgreSQL listens on another address or
                port there.
              </Box>
              <ColumnLayout columns={2}>
                {field("cluster_endpoint", "Database host", "As psql on the SSH host sees it. Usually 127.0.0.1.")}
                {field("port", "Port", "Usually 5432.", "number")}
              </ColumnLayout>
            </SpaceBetween>
          </ExpandableSection>
        </SpaceBetween>
      </Container>
      <Container header={<Header variant="h2" description="The read-only user the tool signs in as. Its password is never sent to the tool or stored by it.">Database user</Header>}>
        <SpaceBetween size="l">
          <RadioGroup
            ariaLabel="Database user"
            value={userMode}
            onChange={({ detail }) => setUserMode(detail.value === "custom" ? "custom" : detail.value === "existing" ? "existing" : "default")}
            items={[
              { value: "default", label: `Create ${DEFAULT_ROLE} for me (recommended)`, disabled: noSudo,
                description: `The tool creates a read-only user named ${DEFAULT_ROLE} on the database server.` },
              { value: "custom", label: "Create a read-only user with another name", disabled: noSudo,
                description: "The same, with a name you choose." },
              { value: "existing", label: "Use an existing user",
                description: "A role you have, with its password in a file on the host or in Secrets Manager." },
            ]}
          />
          {noSudo && (
            <Alert type="info">
              The probe found that the SSH user cannot run commands as postgres, so the tool cannot
              create a user on this host. Use an existing user, or allow sudo as postgres for the
              SSH user and probe again.
            </Alert>
          )}
          {createMode && (
            <SpaceBetween size="m">
              <Box color="text-body-secondary">
                <SpaceBetween size="xxs">
                  <span>
                    Save and check creates the user first, after you confirm. The tool runs one script
                    as the postgres user, through sudo. It makes the password on the server and keeps
                    it in a file there (~/.{role}_pw), so the password never leaves the server.
                    The tool keeps no admin access.
                  </span>
                  <span>
                    Needs: the SSH user can run commands as postgres (sudo), and PostgreSQL 14 or
                    newer. If the user already exists, the tool uses it when its password file is there.
                  </span>
                </SpaceBetween>
              </Box>
              {userMode === "custom" && (
                <ColumnLayout columns={2}>
                  <FormField label="User name" constraintText="Lowercase letters, digits and _." errorText={shown.role_name}>
                    <Input value={customRole} placeholder={DEFAULT_ROLE} onChange={({ detail }) => setCustomRole(detail.value)} />
                  </FormField>
                </ColumnLayout>
              )}
            </SpaceBetween>
          )}
          {roleFail && (
            <Alert type="warning" header={`${roleFail.role_name} was not created`}>
              {roleFail.message} You can choose Use an existing user instead.
            </Alert>
          )}
          {roleDone && !createMode && (
            <Alert type="success" header={roleDone.outcome === "created" ? `Created ${roleDone.role_name}` : `Using ${roleDone.role_name}`}>
              {roleDone.message} The user and password file below are filled in.
            </Alert>
          )}
          {!createMode && (
            <ColumnLayout columns={2}>
              {field("db_user", "Database user", "The read-only role the tool signs in as.")}
              <FormField label="Password source" constraintText={caps.features.secrets_manager ? undefined
                : "This server does not read passwords from Secrets Manager (SECRET_ARN_ALLOWED_PREFIXES is not set)."}>
                <SegmentedControl
                  label="Password source"
                  selectedId={f.password_source}
                  onChange={({ detail }) => setF((x) => ({ ...x, password_source: detail.selectedId === "secret" ? "secret" : "file" }))}
                  options={[{ id: "file", text: "Password file on the host" },
                    { id: "secret", text: "Secrets Manager secret", disabled: !caps.features.secrets_manager }]}
                />
              </FormField>
              {f.password_source === "file"
                ? field("password_file", "Password file", "A file on the database host that holds the password.")
                : field("password_secret_arn", "Secret ARN", "The server reads it with its own AWS credentials and passes it to psql over SSH.")}
            </ColumnLayout>
          )}
        </SpaceBetween>
      </Container>
      <Container header={<Header variant="h2">Name</Header>}>
        <SpaceBetween size="l">
          <FormField label="Label" description="How the database is named in the console. Optional."
            constraintText={`At most ${caps.limits.label_max_length} characters. Leave empty for the database name and host.`} errorText={shown.label}>
            <Input value={f.label} onChange={({ detail }) => setF((x) => ({ ...x, label: detail.value }))} />
          </FormField>
          {creating && (
            <FormField label="Earlier assessments"
              description="Attach earlier assessments with the same database host, port and database name that belong to no saved database. Job records do not keep the SSH host, so turn this on only when they were of this database.">
              <Toggle checked={attachEarlier} onChange={({ detail }) => setAttachEarlier(detail.checked)}>
                {attachEarlier ? "Attach them" : "Do not attach"}{earlier ? " (picked from an earlier assessment)" : ""}
              </Toggle>
            </FormField>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}

/** The readiness check result: each check with its message and hint. */
function CheckView({ check, pending, error, onRetry }: {
  check: DatabaseCheckResponse | null;
  pending: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  return (
    <Container header={<Header variant="h2" actions={<Button iconName="refresh" onClick={onRetry} loading={pending}>Check again</Button>}
      description="SSH, the sign-in, the database host and pg_stat_statements. Nothing is collected.">Readiness</Header>}>
      <SpaceBetween size="m">
        {pending && <Box><Spinner /> Checking the connection. This can take up to a minute.</Box>}
        {error ? <ErrorAlert error={error} header="The check could not run" onRetry={onRetry} /> : null}
        {check && !pending && (
          <SpaceBetween size="s">
            <StatusIndicator type={readinessStatus(check.readiness.status)}>
              {readinessLabel(check.readiness.status)}{check.readiness.database_version ? `, ${check.readiness.database_version}` : ""}
            </StatusIndicator>
            <Box>{READINESS_TEXT[check.readiness.status ?? "unchecked"] ?? ""}</Box>
            <Checklist checks={check.result.checks} />
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}

/**
 * Add a database (choose, sign in, check, save), or edit a saved one (sign in,
 * check, save). The connection is saved when it is checked, because the check runs on
 * a saved database.
 */
export function ConnectWizard({ pick, databaseId }: { pick?: string | undefined; databaseId?: string | undefined }) {
  const caps = useCapabilities();
  const editing = !!databaseId;
  const existing = useDatabase(databaseId ?? "", editing);
  if (caps.error) return <ErrorAlert error={caps.error} header="Server capabilities could not be loaded" onRetry={() => void caps.refetch()} />;
  if (existing.error) return <ErrorAlert error={existing.error} header="The database could not be loaded" onRetry={() => void existing.refetch()} />;
  if (!caps.data || (editing && !existing.data)) return <Box textAlign="center" padding="xl"><Spinner size="large" /></Box>;
  return <ConnectFlow key={databaseId ?? "new"} caps={caps.data} pick={pick} existing={existing.data ?? null} />;
}

function ConnectFlow({ caps, pick, existing }: { caps: Capabilities; pick: string | undefined; existing: DatabaseView | null }) {
  const editing = !!existing;
  const ids: StepId[] = editing ? ["signin", "check", "save"] : ["choose", "signin", "check", "save"];
  const create = useCreateDatabase();
  const update = useUpdateDatabase();
  const check = useCheckDatabase();
  const del = useDeleteDatabase();
  const roleSetup = useCreateRole();
  const notify = useNotify();

  const [active, setActive] = useState(0);
  const [mode, setMode] = useState<"found" | "manual">(pick ? "found" : "manual");
  // Discovery runs only when the user picks from it (shared with Get started's result).
  const disc = useDiscovery(!editing && mode === "found");
  const [found, setFound] = useState<DiscoveredServer | null>(null);
  const [f, setF] = useState<ConnectionFields>(() => existing ? fieldsFromConnection(existing.connection, existing.label) : EMPTY_CONNECTION);
  const [touched, setTouched] = useState(false);
  const [attachEarlier, setAttachEarlier] = useState(false);
  // What is saved now: the id, the connection and the label, to know when to PATCH.
  const [saved, setSaved] = useState<{ id: string; key: string; label: string } | null>(() =>
    existing ? { id: existing.database_id, key: connectionKey(existing.connection), label: existing.label } : null);
  const [createdHere, setCreatedHere] = useState(false);
  const [checked, setChecked] = useState<{ key: string; result: DatabaseCheckResponse } | null>(null);
  const [checkKey, setCheckKey] = useState<string | null>(null);
  const [stepError, setStepError] = useState<unknown>(null);
  const [cancelling, setCancelling] = useState(false);
  // The database user: the tool creates apcat_reader (default) or another name, or one the user has.
  const [userMode, setUserMode] = useState<UserMode>(() => initialUserMode(editing, null));
  const [customRole, setCustomRole] = useState("");
  const [access, setAccess] = useState<"sudo" | "no_sudo" | "failed" | "not_checked" | null>(null);
  const [confirmRole, setConfirmRole] = useState(false);
  const [roleDone, setRoleDone] = useState<RoleCreateResult | null>(null);
  const [roleFail, setRoleFail] = useState<RoleCreateResult | null>(null);

  const step = ids[active] ?? "signin";
  const servers = useMemo(() => disc.data?.servers ?? [], [disc.data]);
  const { databases: clusters } = useMemo(() => splitFinds(servers), [servers]);
  const errs = signInErrors(f, caps, userMode, customRole);
  const shown = touched ? errs : {};
  const discoveredFrom: DiscoverySource | null = mode === "found" && found ? found.source : null;

  /** Take a find: its fields fill the form, keeping a label already typed. */
  const choose = (s: DiscoveredServer) => {
    setFound(s);
    setF((x) => ({ ...fieldsFromDiscovery(s), label: x.label }));
    setAttachEarlier(s.source === "earlier_assessment");
    // A probe that listed databases through sudo, or found that sudo does not work.
    setAccess(s.postgres_access ?? null);
    setUserMode(initialUserMode(false, s.postgres_access));
    setRoleDone(null);
    setRoleFail(null);
  };

  // Opened from Get started with a find: take it and go straight to sign in.
  useEffect(() => {
    if (!pick || found || editing) return;
    const s = servers.find((x) => x.id === pick);
    if (s) { setMode("found"); choose(s); setActive(1); }
  }, [pick, servers]); // eslint-disable-line react-hooks/exhaustive-deps

  const runCheck = (id: string, key: string) => {
    setChecked(null);
    setCheckKey(key);
    check.mutate(id, { onSuccess: (r) => setChecked({ key, result: r }) });
  };

  /**
   * Save the connection (create, or patch what changed), then check it.
   * @param fields The fields to save: the form's, or the form's with a user just created.
   */
  const saveAndCheck = async (fields: ConnectionFields = f): Promise<boolean> => {
    setTouched(true);
    if (Object.keys(connectionErrors(fields, caps)).length) return false;
    setStepError(null);
    const connection = toConnection(fields);
    const key = connectionKey(connection);
    const label = fields.label.trim();
    try {
      let id: string;
      if (!saved) {
        const r = await create.mutateAsync({
          connection, ...(label ? { label } : {}), ...(discoveredFrom ? { discovered_from: discoveredFrom } : {}),
          ...(attachEarlier ? { attach_earlier: true } : {}),
        });
        id = r.database_id;
        setCreatedHere(true);
        setSaved({ id, key, label: r.label });
        if (!label) setF((x) => ({ ...x, label: r.label }));
        if ((r.attached ?? 0) > 0) notify({ type: "info", content: `${r.attached} earlier assessment${r.attached === 1 ? " was" : "s were"} attached.` });
      } else {
        id = saved.id;
        const changed = key !== saved.key;
        const relabel = !!label && label !== saved.label;
        if (changed || relabel) {
          const r = await update.mutateAsync({ databaseId: id, body: { ...(changed ? { connection } : {}), ...(relabel ? { label } : {}) } });
          setSaved({ id, key, label: r.label });
        }
      }
      // Check once per connection; Check again on the next step runs it again.
      if (checkKey !== key) runCheck(id, key);
      return true;
    } catch (e) {
      setStepError(e);
      return false;
    }
  };

  /** Create the user the tool signs in as (after the confirm), fill it in, then save and check. */
  const createThenSave = async () => {
    setConfirmRole(false);
    setRoleFail(null);
    setRoleDone(null);
    setStepError(null);
    let r: RoleCreateResult;
    try {
      r = await roleSetup.mutateAsync(roleRequest(f, userMode, customRole));
    } catch (e) {
      setStepError(e);
      return;
    }
    if (!r.ok) { setRoleFail(r); return; }
    const next: ConnectionFields = {
      ...f, db_user: r.role_name, password_source: "file", password_file: r.password_file ?? "", password_secret_arn: "",
    };
    setF(() => next);
    setRoleDone(r);
    setUserMode("existing");
    if (await saveAndCheck(next)) setActive(active + 1);
  };

  const finish = async () => {
    if (!saved) return;
    const label = f.label.trim();
    try {
      if (label && label !== saved.label) await update.mutateAsync({ databaseId: saved.id, body: { label } });
    } catch (e) { setStepError(e); return; }
    notify({ type: "success", content: editing ? "The database was updated." : "The database was saved." });
    navigate({ name: "database", id: saved.id });
  };

  const leave = () => navigate(editing && saved ? { name: "database", id: saved.id } : { name: "home" });

  const onNavigate = async (requested: number) => {
    if (requested <= active) { setStepError(null); setActive(requested); return; }
    // Forward one step at a time, so a changed connection always goes through Save and check.
    const to = active + 1;
    if (step === "choose") {
      if (mode === "found" && !found) { setStepError("pick"); return; }
      if (mode === "manual") setFound(null);
      setStepError(null);
      setActive(to);
      return;
    }
    if (step === "signin") {
      if (userMode !== "existing") {
        // Creating the user comes first, after a confirm; the confirm saves and checks.
        setTouched(true);
        if (Object.keys(errs).length) return;
        setStepError(null);
        setConfirmRole(true);
        return;
      }
      if (await saveAndCheck()) setActive(to);
      return;
    }
    setActive(to);
  };

  const conflict = existingId(stepError);
  const errorText = stepError === "pick" ? "Select a database or instance from the list, or choose Enter the details."
    : stepError ? (
      <SpaceBetween size="xs">
        <ErrorAlert error={stepError} header="The connection was not saved" />
        {conflict && <Link href={href({ name: "database", id: conflict })} onFollow={(e) => follow(e)}>Open the saved database</Link>}
      </SpaceBetween>
    ) : undefined;

  const c = saved ? toConnection(f) : null;
  const steps = ids.map((id) => {
    switch (id) {
      case "choose":
        return {
          title: "Choose",
          description: "Use a database or instance that discovery or a host probe found, or enter the connection details yourself.",
          errorText: step === "choose" ? errorText : undefined,
          content: (
            <SpaceBetween size="l">
              <Tiles value={mode} onChange={({ detail }) => setMode(detail.value === "found" ? "found" : "manual")}
                ariaLabel="How to connect"
                items={[
                  { value: "found", label: "Pick a database or instance", description: "On this machine, in pg_service.conf, or on a host you probe." },
                  { value: "manual", label: "Enter the details", description: "Give the SSH host and the database yourself." },
                ]} />
              {mode === "found" && (
                <SpaceBetween size="l">
                  <Container header={<Header variant="h2" counter={disc.data ? `(${clusters.length})` : undefined}
                    description="Pick one. For an instance, name the database to assess on the next step."
                    actions={<Button iconName="refresh" loading={disc.isFetching} onClick={() => void disc.refetch()}>Look again</Button>}>
                    Databases and instances</Header>}>
                    {disc.error ? <ErrorAlert error={disc.error} header="Discovery could not run" /> : null}
                    <FoundTable items={clusters} selected={found} onSelect={choose} loading={disc.isFetching && !disc.data}
                      label="Databases and instances" empty="No databases or instances found. Probe a host below to look for PostgreSQL on it." />
                  </Container>
                  <HostProbePanel description="Probe an SSH host to add what runs on it to the list above. When the SSH user can run commands as postgres (sudo), the probe lists the databases." />
                </SpaceBetween>
              )}
            </SpaceBetween>
          ),
        };
      case "signin":
        return {
          title: "Sign in",
          description: "How the tool reaches the database and signs in. Next saves the connection and checks it.",
          errorText: step === "signin" ? errorText : undefined,
          content: <SignInForm caps={caps} f={f} setF={setF} shown={shown}
            note={mode === "found" && found ? discoveryNote(found) : null} creating={!saved}
            attachEarlier={attachEarlier} setAttachEarlier={setAttachEarlier} earlier={found?.source === "earlier_assessment"}
            userMode={userMode} setUserMode={(m) => { setUserMode(m); setRoleFail(null); }}
            customRole={customRole} setCustomRole={setCustomRole}
            access={access} setAccess={setAccess} roleDone={roleDone} roleFail={roleFail} />,
        };
      case "check":
        return {
          title: "Check readiness",
          description: "The same checks a preflight runs, on the saved connection.",
          content: <CheckView check={checked?.result ?? null} pending={check.isPending} error={check.error}
            onRetry={() => saved && runCheck(saved.id, saved.key)} />,
        };
      default:
        return {
          title: "Save",
          description: "Review the connection. You can run an assessment from the database page.",
          errorText: step === "save" ? errorText : undefined,
          content: (
            <Container header={<Header variant="h2">Review</Header>}>
              <KeyValuePairs columns={3} items={[
                { label: "Label", value: f.label.trim() || saved?.label || "-" },
                { label: "SSH target", value: c ? `${c.ssh_target}, port ${c.ssh_port}` : "-" },
                { label: "Key name", value: c?.identity_file || "Server default" },
                { label: "Database", value: c ? `${c.database_name} at ${c.cluster_endpoint}:${c.port}` : "-" },
                { label: "Database user", value: c?.db_user ?? "-" },
                { label: "Password source", value: c?.password_secret_arn ? `Secrets Manager: ${c.password_secret_arn}` : `File on the host: ${c?.password_file ?? "-"}` },
                { label: "Readiness", value: checked
                  ? <StatusIndicator type={readinessStatus(checked.result.readiness.status)}>{readinessLabel(checked.result.readiness.status)}</StatusIndicator>
                  : <StatusIndicator type="not-started">Not checked</StatusIndicator> },
              ]} />
            </Container>
          ),
        };
    }
  });

  return (
    <>
      <Wizard
        steps={steps}
        activeStepIndex={active}
        onNavigate={({ detail }) => void onNavigate(detail.requestedStepIndex)}
        onCancel={() => (createdHere ? setCancelling(true) : leave())}
        onSubmit={() => void finish()}
        isLoadingNextStep={create.isPending || update.isPending || roleSetup.isPending}
        i18nStrings={{
          stepNumberLabel: (n) => `Step ${n}`,
          collapsedStepsLabel: (n, total) => `Step ${n} of ${total}`,
          navigationAriaLabel: "Steps",
          cancelButton: "Cancel",
          previousButton: "Previous",
          nextButton: step !== "signin" ? "Next" : userMode === "existing" ? "Save and check" : "Create user, save and check",
          submitButton: editing ? "Save changes" : "Save database",
          optional: "optional",
        }}
      />
      <Modal visible={confirmRole} onDismiss={() => setConfirmRole(false)} header="Create a read-only user?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setConfirmRole(false)}>Cancel</Button>
              <Button variant="primary" onClick={() => void createThenSave()}>Create user</Button>
            </SpaceBetween>
          </Box>
        }>
        Create the read-only user <b>{roleFor(userMode, customRole) || DEFAULT_ROLE}</b> in <b>{f.database_name}</b> on <b>{f.ssh_target}</b>?
        The tool runs this once as the postgres user, through sudo, and keeps no admin access.
        The user can read statistics and data but cannot change data or settings.
        Then the tool saves the connection and checks it.
      </Modal>
      <Modal
        visible={cancelling}
        onDismiss={() => setCancelling(false)}
        header="Keep the saved connection?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setCancelling(false)}>Go back</Button>
              <Button loading={del.isPending} onClick={() => saved && del.mutate(saved.id, {
                onSuccess: () => { setCancelling(false); navigate({ name: "home" }); },
              })}>Delete it</Button>
              <Button variant="primary" onClick={() => saved && navigate({ name: "database", id: saved.id })}>Keep it</Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>The connection was saved when it was checked. Keep it to check or run it later, or delete it.</Box>
          {del.error ? <ErrorAlert error={del.error} header="Delete failed" /> : null}
        </SpaceBetween>
      </Modal>
    </>
  );
}
