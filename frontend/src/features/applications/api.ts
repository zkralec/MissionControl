import { apiRequest } from "@/lib/api/client";
import type { TaskOut, TaskResultOut } from "@/lib/api/generated/openapi";

export type DraftSummaryJobInput = {
  job_id?: string | null;
  title?: string | null;
  company?: string | null;
  source_url?: string | null;
  url?: string | null;
};

export type DraftSummary = {
  job_id?: string | null;
  title?: string | null;
  company?: string | null;
  job_url?: string | null;
  idempotency_key?: string | null;
  state: string;
  state_label: string;
  review_status?: string | null;
  awaiting_review: boolean;
  can_create: boolean;
  can_review: boolean;
  prepare_task_id?: string | null;
  resume_task_id?: string | null;
  draft_task_id?: string | null;
  pipeline_id?: string | null;
  current_task_type?: string | null;
  current_task_status?: string | null;
  submitted: boolean;
};

export type CreateApplicationDraftInput = {
  shortlist_task_id: string;
  shortlist_run_id: string;
  selected_job: Record<string, unknown>;
  request?: Record<string, unknown>;
  prepare_policy?: Record<string, unknown>;
  max_attempts?: number;
};

export type ReviewApplicationDraftInput = {
  action: "approve" | "reject" | "mark_reviewed";
  reviewer?: string | null;
  notes?: string | null;
};

export function getApplicationDraftSummaries(jobs: DraftSummaryJobInput[]): Promise<DraftSummary[]> {
  return apiRequest<DraftSummary[]>("/applications/drafts/summary", {
    method: "POST",
    body: { jobs }
  });
}

export function createApplicationDraft(input: CreateApplicationDraftInput): Promise<TaskOut> {
  return apiRequest<TaskOut>("/applications/drafts", {
    method: "POST",
    body: input
  });
}

export function reviewApplicationDraft(taskId: string, input: ReviewApplicationDraftInput): Promise<TaskResultOut> {
  return apiRequest<TaskResultOut>(`/applications/drafts/${encodeURIComponent(taskId)}/review`, {
    method: "POST",
    body: input
  });
}
