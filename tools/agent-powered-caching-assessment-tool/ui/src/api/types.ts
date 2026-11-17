// Names for the generated schema types. Nothing here is hand-typed: every shape comes
// from src/api/schema.d.ts, which `npm run gen:api` writes from the backend's OpenAPI.
import type { components, operations } from "./schema";

type S = components["schemas"];

export type Capabilities = S["Capabilities"];
export type OptionInfo = S["OptionInfo"];
export type PhaseInfo = S["PhaseInfo"];
export type ParameterInfo = S["ParameterInfo"];

export type CollectorInput = S["CollectorInput"];
export type LiveConnectionConfig = S["LiveConnectionConfig"];
export type PreflightRequest = S["PreflightRequest"];
export type PreflightResult = S["PreflightResult"];
export type PreflightCheck = S["PreflightCheck"];
export type RunFromCollectionRequest = S["RunFromCollectionRequest"];
export type RerunRequest = S["RerunRequest"];
export type SubmitResponse = S["SubmitResponse"];

export type JobState = S["JobStatus"]["status"];
export type JobStatus = S["JobStatus"];
export type PhaseStatus = S["PhaseStatus"];
export type JobList = S["JobList"];
export type JobSummary = S["JobSummary"];
export type CancelResponse = S["CancelResponse"];

export type AssessmentSummary = S["AssessmentSummary"];
export type ReportOverview = S["ReportOverview"];
export type ReportMeta = S["ReportMeta"];
export type Headline = S["Headline"];
export type QueryItem = S["QueryItem"];
export type TableItem = S["TableItem"];
export type QueryPage = S["QueryPage"];
export type TablePage = S["TablePage"];
export type MetricSet = S["MetricSet"];
export type Fact = S["Fact"];
export type BundleItem = S["BundleItem"];
export type NotCacheableGroup = S["NotCacheableGroup"];
export type NotCacheableItem = S["NotCacheableItem"];
export type RuledOutTable = S["RuledOutTable"];
export type ScoringSection = S["ScoringSection"];

export type CacheSizingOutput = S["CacheSizingOutput"];
export type SizedItem = S["SizedItem"];
export type SizingOverrides = S["SizingOverrides"];
export type WhatIfResult = S["WhatIfResult"];
export type SizingWhatIf = S["SizingWhatIf"];

export type ApiErrorBody = S["ApiErrorBody"];

// Discovery (POST /databases/discover).
export type DiscoverRequest = S["DiscoverRequest"];
export type ProbeHost = S["ProbeHost"];
export type HostTarget = S["HostTarget"];
export type HostProbeResult = S["HostProbeResult"];
export type ProbeStage = S["ProbeStage"];
export type HostKeyInfo = S["HostKeyInfo"];
export type HostTrustRequest = S["HostTrustRequest"];
export type HostTrustResult = S["HostTrustResult"];
export type SshKeyInfo = S["SshKeyInfo"];
export type SshKeyList = S["SshKeyList"];
export type HostSuggestion = S["HostSuggestion"];
export type RoleCreateRequest = S["RoleCreateRequest"];
export type RoleCreateResult = S["RoleCreateResult"];
export type DiscoveryResult = S["DiscoveryResult"];
export type DiscoveredServer = S["DiscoveredServer"];
export type SourceReport = S["SourceReport"];
export type DiscoverySource = DiscoveredServer["source"];

// Saved databases (/databases).
export type DatabaseConnection = S["DatabaseConnection"];
export type DatabaseCreate = S["DatabaseCreate"];
export type DatabaseCreated = S["DatabaseCreated"];
export type DatabasePatch = S["DatabasePatch"];
export type DatabaseView = S["DatabaseView"];
export type DatabaseList = S["DatabaseList"];
export type DatabaseCheckResponse = S["DatabaseCheckResponse"];
export type DatabaseRunRequest = S["DatabaseRunRequest"];
export type ReadinessState = S["ReadinessState"];
export type Readiness = NonNullable<ReadinessState["status"]>;
export type LatestAssessment = S["LatestAssessment"];

export type ListAssessmentsQuery = NonNullable<operations["list_assessments"]["parameters"]["query"]>;
export type CreateAssessmentQuery = NonNullable<
  operations["create_assessment"]["parameters"]["query"]
>;
