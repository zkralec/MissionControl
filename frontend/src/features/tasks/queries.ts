import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createTask, getTask, getTaskResult, getTaskRuns, listRuns, listTasks, type CreateTaskInput } from "@/features/tasks/api";

export const taskQueryKeys = {
  tasks: (limit: number) => ["tasks", { limit }] as const,
  runs: (limit: number) => ["runs", { limit }] as const,
  task: (taskId: string) => ["task", taskId] as const,
  taskRuns: (taskId: string, limit: number) => ["task-runs", taskId, { limit }] as const,
  taskResult: (taskId: string) => ["task-result", taskId] as const
};

const terminalStatuses = new Set(["success", "failed", "failed_permanent", "blocked_budget"]);

export function useTasks(limit = 100) {
  return useQuery({ queryKey: taskQueryKeys.tasks(limit), queryFn: () => listTasks(limit), refetchInterval: 5_000 });
}

export function useRuns(limit = 200) {
  return useQuery({ queryKey: taskQueryKeys.runs(limit), queryFn: () => listRuns(limit), refetchInterval: 5_000 });
}

export function useTask(taskId: string | null) {
  return useQuery({
    queryKey: taskQueryKeys.task(taskId || ""),
    queryFn: () => getTask(taskId || ""),
    enabled: Boolean(taskId),
    refetchInterval: (query) => {
      const status = String((query.state.data as { status?: string } | undefined)?.status || "").toLowerCase();
      return terminalStatuses.has(status) ? false : 5_000;
    }
  });
}

export function useTaskRuns(taskId: string | null, limit = 20) {
  return useQuery({
    queryKey: taskQueryKeys.taskRuns(taskId || "", limit),
    queryFn: () => getTaskRuns(taskId || "", limit),
    enabled: Boolean(taskId),
    refetchInterval: 5_000
  });
}

export function useTaskResult(taskId: string | null) {
  return useQuery({
    queryKey: taskQueryKeys.taskResult(taskId || ""),
    queryFn: () => getTaskResult(taskId || ""),
    enabled: Boolean(taskId),
    retry: false,
    refetchInterval: (query) => {
      return query.state.data ? false : 5_000;
    }
  });
}

export function useCreateTaskMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateTaskInput) => createTask(input),
    onSuccess: (task) => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: taskQueryKeys.task(task.id) });
    }
  });
}
