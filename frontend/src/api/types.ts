export interface Agent {
  id: string;
  name: string;
  created_at?: string;
}
export type SandboxLevel = "local" | "container";
export type SandboxPreset = "conservative" | "coding";
export interface SandboxLimits {
  timeout_seconds: number;
  memory_bytes: number;
  max_output_bytes: number;
  session_timeout_seconds: number;
  max_sessions: number;
}
// Frozen shape from GET/PUT /sandbox. protections/limitations are the backend's
// own honest wording and are shown to the owner verbatim.
export interface SandboxStatus {
  level: SandboxLevel;
  available_levels: SandboxLevel[];
  engine: { available: boolean; name: "docker" | "podman" | null };
  protections: string[];
  limitations: string[];
  image: string;
  preset?: SandboxPreset;
  limits?: SandboxLimits;
}
export interface AgentPermissions {
  agent_id: string;
  revision: number;
  readable: string[];
  writable: string[];
}
export interface PermissionUpdate {
  readable: string[];
  writable: string[];
  expected_revision: number;
}
export interface AgentInstructions {
  agent_id: string;
  rules: string[];
  defaults: string[];
  customized: boolean;
}
export interface AgentUsage {
  scope: "chat" | "all";
  categories?: Record<string, number>;
  calls: number;
  reported_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  provider_total_tokens: number | null;
  cost_usd: string | null;
  cost_status: "unknown" | "estimated" | "partial";
  estimated_cost_usd?: string | null;
  priced_calls?: number;
  note: string;
}
export interface Attachment {
  id: string;
  original_name: string;
  byte_count: number;
  status: "ready" | "partial" | "needs_vision";
  segments: { location: string; text: string }[];
  metadata: Record<string, unknown>;
  warnings: string[];
}
export interface ModelCapabilities {
  streaming: boolean;
  tools: boolean;
  thinking: boolean;
  effort_levels: string[];
  encrypted_reasoning?: boolean;
  images?: boolean;
  pdf?: boolean;
  temperature?: boolean;
  top_p?: boolean;
  prompt_caching?: boolean;
}
export interface ModelProfile {
  api_family:
    "anthropic_messages" | "openai_chat_completions" | "openai_responses";
  model: string;
  base_url: string;
  capabilities: ModelCapabilities;
  context_window_tokens?: number | null;
  max_output_tokens?: number | null;
  default_effort?: string | null;
  temperature?: number | null;
  top_p?: number | null;
  cache_system_prompt?: boolean;
  // Pydantic serializes Decimal prices as strings; form submissions use numbers.
  input_price_per_million?: number | string | null;
  output_price_per_million?: number | string | null;
  [key: string]: unknown;
}
export interface SavedModel {
  id: string;
  name: string;
  profile: ModelProfile;
  credentials_available: boolean;
}
export interface ModelSelection {
  primary: string | null;
  fallback: string | null;
}

// One entry in the owner-managed MCP server list (the mcp.servers setting).
// Disabled entries are kept with enabled: false; config changes take effect on
// the next use (the service reloads).
export interface McpServerEntry {
  name: string;
  transport: "stdio" | "http";
  enabled?: boolean;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
  [key: string]: unknown;
}

export interface SkillpackSource {
  source: string;
  path: string;
  exists: boolean;
}

// Read-only view of the scanned skill folders: what exists, where from.
export interface SkillpackView {
  roots: SkillpackSource[];
  skills: { name: string; description: string; source: string }[];
}
export interface ModelInput {
  name: string;
  profile: ModelProfile;
  profile_id?: string;
  api_key?: string;
}

export interface ChatRequest {
  agent_id: string;
  message: string;
  profile_id?: string;
  request_id: string;
  effort?: string;
  conversation_id?: string;
  regenerate_message_id?: string;
  attachment_ids?: string[];
}

export interface Conversation {
  id: string;
  agent_id: string;
  title: string;
  scope: string;
  head_id: string | null;
  created_at?: string;
  background?: string;
}

/** The server-owned continuity boundary for one individual. */
export interface MainConversation {
  conversation: Conversation | null;
  revision: number;
  choices: Conversation[];
}

export type WorkStatus =
  | "queued"
  | "starting"
  | "running"
  | "paused"
  | "waiting"
  | "finishing"
  | "completed"
  | "failed"
  | "cancelled"
  | "unknown"
  | string;

/** A task works in its own conversation while the main conversation keeps chatting. */
export interface WorkBranch {
  id: string;
  agent_id: string;
  conversation_id: string;
  main_conversation_id: string;
  origin_message_id: string | null;
  objective: string;
  status: WorkStatus;
  revision: number;
  run_id?: string | null;
  outcome?: string | null;
  delivery_state?: string | null;
  archive_id?: string | null;
  return_state?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_code?: string | null;
  paused?: boolean;
  finish_requested?: boolean;
  queued_reason?: string | null;
}

export type ExperienceKind = "work_branch" | "main_event" | "work_event";

/** A first-class item in the shared, read-only experience directory. */
export interface Experience {
  id: string;
  agent_id: string;
  kind: ExperienceKind | string;
  title: string;
  summary: string;
  revision: number;
  occurred_start?: string | null;
  occurred_end?: string | null;
  coverage: string;
  valid: boolean;
  reason?: string | null;
  conversation_id?: string | null;
  branch_id?: string | null;
  source_count: number;
}

export interface ExperiencePage {
  items: Experience[];
  next_cursor?: string | null;
}

export interface ExperienceRecord {
  id: string;
  source_id: string;
  kind: string;
  location: string;
  coverage: string;
  byte_count?: number | null;
  sha256?: string;
}

export interface ExperienceRecordPage {
  items: ExperienceRecord[];
  next_cursor?: string | null;
  revision: number;
}

export interface ExperienceRecordText {
  text: string;
  next_cursor?: string | null;
  sha256: string;
  page_bytes: number;
  revision: number;
  coverage: string;
}

export interface ReflectionPolicy {
  enabled: boolean;
  revision: number;
  local_time: string;
  timezone: string;
  min_age_days: number;
  threshold_bytes: number;
  profile_id: string | null;
  max_calls: number;
  max_source_bytes: number;
  max_events: number;
  daily_call_limit: number;
  daily_calls_used?: number;
  quality_status?: string;
  last_tick_at?: string | null;
  next_run_at?: string | null;
}

export interface ReflectionPreview {
  gate_passed: boolean;
  source_units: number;
  eligible_units: number;
  candidate_bytes: number;
  cutoff: string | null;
  threshold_bytes: number;
  reason?: string | null;
  model_calls: number;
  deferred?: { reason?: string; [key: string]: unknown }[];
  coverage?: string | null;
}

export interface ReflectionRun {
  id: string;
  agent_id?: string;
  status: string;
  outcome?: string | null;
  error_code?: string | null;
  request_id: string;
  revision?: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  calls_used?: number;
  calls_limit?: number;
  calls?: number;
  max_calls?: number;
  usage?: Record<string, unknown> | null;
  input_bytes?: number | null;
  output_bytes?: number | null;
  event_count?: number;
  cancel_requested?: boolean;
  detail?: string | null;
  quality_status?: string | null;
  candidate_events?: ReflectionCandidate[];
}

export interface ReflectionCandidate {
  title: string;
  summary: string;
  source_ids: string[];
  occurred_start?: string | null;
  occurred_end?: string | null;
}

export interface ReflectionQualification {
  profile_id: string;
  revision: number;
  qualified: boolean;
  sample_count: number;
  acceptable_count: number;
  critical_errors: number;
  evidence: string;
  valid?: boolean;
  status?: string;
}

export type CompactionMode =
  | "disabled"
  | "safe_epoch_restart"
  | "native_compaction";

export interface CompactionSettings {
  enabled: boolean;
  revision: number;
  profile_modes: Record<string, CompactionMode>;
  trigger_ratio: number;
  target_ratio: number;
  max_summary_bytes?: number;
  max_epochs?: number;
  validation?: string;
  native_compaction_available?: boolean;
  hard_checkpoint_bytes?: number;
  /** Reported by the server; the UI keeps offline and live verification apart. */
  profile_verification?: Record<
    string,
    { offline_verified?: boolean; live_verified?: boolean; note?: string }
  >;
}

// Per-conversation access mode: "full" releases gated calls (commands, remote
// tools) without waiting for a per-call owner confirmation; every other control
// (sandbox, audit, grants) is unchanged. Absent setting reads back "confirm".
export interface ConversationAccess {
  conversation_id: string;
  mode: "confirm" | "full";
}

// Messages hold their own text; the selected branch is the chain up to the head.
export interface ConversationMessage {
  id: string;
  parent_id: string | null;
  role: "user" | "assistant";
  text: string;
  attachment_ids?: string[];
  created_at?: string;
}

export interface ConversationBranch extends ConversationMessage {
  selected: boolean;
}

export interface ChatUsage {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  [key: string]: unknown;
}

// One tool call inside the chat loop. It starts as "running" on tool_started and
// is completed by the matching tool_result (same call_id). A call released
// without a per-call confirmation by the conversation's full-access mode
// carries access: "full".
export interface ChatToolCall {
  step: number;
  name: string;
  call_id: string;
  arguments: Record<string, unknown>;
  status?: string;
  preview?: string;
  access?: "full";
  verification?: VerificationEvidence;
  execution?: ExecutionSession;
  executionAction?: "refreshing" | "cancelling";
}

export type VerificationStatus =
  "unverified" | "stale" | "passed" | "failed" | "unknown";
export interface VerificationCheck {
  argv: string[];
  exit_code: number;
}
export interface VerificationEvidence {
  status: VerificationStatus;
  kind?: "command" | "content";
  checks?: VerificationCheck[];
}
export interface ExecutionSession {
  session_id: string;
  status: string;
  cursor: number;
  output?: string;
  truncated?: boolean;
  result?: Record<string, unknown>;
}

// A side-effecting call waiting for the owner's decision (run_command).
export interface PendingConfirmation {
  run_id: string;
  fingerprint: string;
  tool: string;
  arguments?: Record<string, unknown>;
  message?: string;
}

export type ChatEvent = { id: string } & (
  | {
      event: "started";
      data: {
        run_id: string;
        conversation_id?: string | null;
        user_message_id?: string | null;
      };
    }
  | {
      event: "model_switch";
      data: {
        run_id: string;
        from_profile: string;
        to_profile: string;
        restart: boolean;
      };
    }
  | {
      event: "notice";
      data: {
        run_id: string;
        code: string;
        message?: string;
        fingerprint?: string;
        tool?: string;
        arguments?: Record<string, unknown>;
      };
    }
  | { event: "step_started"; data: { run_id: string; step: number } }
  | {
      event: "tool_started";
      data: {
        run_id: string;
        step: number;
        name: string;
        call_id: string;
        arguments: Record<string, unknown>;
      };
    }
  | {
      event: "tool_result";
      data: {
        run_id: string;
        step: number;
        name: string;
        call_id: string;
        status: string;
        preview: string;
        access?: "full";
        verification?: VerificationEvidence;
        execution?: ExecutionSession;
      };
    }
  | { event: "paused" | "resumed"; data: { run_id: string } }
  | { event: "text" | "thinking"; data: { run_id: string; text: string } }
  | { event: "usage"; data: { run_id: string; usage: ChatUsage } }
  | {
      event: "complete";
      data: {
        run_id: string;
        status: "completed";
        assistant_message_id: string | null;
        verification?: VerificationEvidence;
      };
    }
  | { event: "error"; data: { run_id: string; code: string; message: string } }
);

export interface ChatRun {
  id: string;
  agent_id: string;
  request_id: string;
  profile_id: string;
  status: "running" | "completed" | "failed" | "cancelled" | "interrupted";
  conversation_id?: string | null;
  user_message_id?: string | null;
  context_manifest?: Record<string, unknown> | null;
  created_at: string;
  finished_at: string | null;
  verification?: VerificationEvidence;
  recoverable?: boolean;
  recovery_reason?: string;
}
