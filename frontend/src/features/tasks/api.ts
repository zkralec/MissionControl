import { ApiError, apiRequest } from "@/lib/api/client";
import type { RunOut, TaskOut, TaskResultOut } from "@/lib/api/generated/openapi";

export type CreateTaskInput = {
  task_type: string;
  payload_json: string;
  model?: string | null;
  idempotency_key?: string | null;
  max_attempts?: number;
  max_cost_usd?: number | null;
  expected_tokens_in?: number | null;
  expected_tokens_out?: number | null;
};

export function listTasks(limit = 100): Promise<TaskOut[]> {
  return apiRequest<TaskOut[]>(`/tasks?limit=${encodeURIComponent(limit)}`);
}

export function listRuns(limit = 200): Promise<RunOut[]> {
  return apiRequest<RunOut[]>(`/runs?limit=${encodeURIComponent(limit)}`);
}

export function getTask(taskId: string): Promise<TaskOut> {
  return apiRequest<TaskOut>(`/tasks/${encodeURIComponent(taskId)}`);
}

export function getTaskRuns(taskId: string, limit = 50): Promise<RunOut[]> {
  return apiRequest<RunOut[]>(`/tasks/${encodeURIComponent(taskId)}/runs?limit=${encodeURIComponent(limit)}`);
}

export async function getTaskResult(taskId: string): Promise<TaskResultOut | null> {
  try {
    return await apiRequest<TaskResultOut>(`/tasks/${encodeURIComponent(taskId)}/result`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

export function createTask(input: CreateTaskInput): Promise<TaskOut> {
  return apiRequest<TaskOut>("/tasks", { method: "POST", body: input });
}
