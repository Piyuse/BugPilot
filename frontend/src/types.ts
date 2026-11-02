export type Status = "queued" | "running" | "pr_created" | "needs_attention";

export interface Session {
  authenticated: boolean;
  username: string;
  can_manage: boolean;
  mode: "live";
}
export interface Repository {
  id: number;
  name: string;
  url: string;
  default_branch: string;
  description: string;
  is_active: boolean;
  validation_commands: string[][];
  runtime_image: string;
  created_at: string;
}
export interface Investigation {
  id: string;
  issue_source: "github" | "jira";
  source_repository_url: string;
  source_issue_number: number | null;
  issue_key: string;
  issue_url: string;
  title: string;
  requested_repository: string | null;
  repository: Repository | null;
  mode: "live";
  status: Status;
  stage: string;
  selection_reason: string;
  branch: string;
  base_branch: string;
  publication_repository_url: string;
  publication_head_owner: string;
  pull_request_url: string;
  issue_linked: boolean;
  issue_comment_id: string;
  issue_comment_url: string;
  model_calls: Partial<Record<'preprocessing' | 'coding', { provider: string; model: string; completed_at: string }>>;
  error: string;
  attempt: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}
export interface InvestigationDetail extends Investigation {
  ticket: Record<string, unknown>;
  coding_brief: string;
  preprocessed: Partial<{
    summary: string;
    expected_behavior: string;
    actual_behavior: string;
    error_details: string;
    reproduction_steps: string[];
    keywords: string[];
  }>;
  proposal: Partial<{
    summary: string;
    root_cause: string;
    tests_added: string[];
    diff: string;
  }>;
  validation: Partial<{
    passed: boolean;
    reason: string;
    checks: { command: string[]; passed: boolean; output: string }[];
    changed_files: string[];
  }>;
  events: { id: number; stage: string; message: string; created_at: string }[];
}
export interface Overview {
  mode: "live";
  counts: Partial<Record<Status, number>>;
  worker_online: boolean;
  worker_last_seen: string | null;
  stages: { key: string; label: string }[];
  pipeline: {
    ready: boolean;
    missing: string[];
    preprocessing: { provider: string; model: string };
    coding: { provider: string; model: string };
  };
  integrations: { name: string; description: string; configured: boolean }[];
  username: string;
  can_manage: boolean;
}
export interface Paginated<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}
