import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import {
  createPlannerTemplate,
  deletePlannerTemplate,
  getPlannerConfig,
  listPlannerTemplates,
  patchPlannerConfig,
  patchPlannerTemplate,
  resetPlannerConfig,
  runPlannerOnce,
  saveJobsPreset,
  saveRtx5090Preset,
  type PlannerConfigPatchInput,
  type PlannerTemplateCreateInput
} from "@/features/planner/api";
import { getPlannerStatus } from "@/features/telemetry/api";

export const plannerKeys = {
  config: ["planner", "config"] as const,
  templates: (limit: number) => ["planner", "templates", { limit }] as const,
  status: ["planner", "status"] as const
};

function invalidatePlannerQueries(queryClient: ReturnType<typeof useQueryClient>): void {
  queryClient.invalidateQueries({ queryKey: ["planner"] });
  queryClient.invalidateQueries({ queryKey: ["telemetry", "planner-status"] });
  queryClient.invalidateQueries({ queryKey: ["watchers"] });
}

export function usePlannerConfig() {
  return useQuery({
    queryKey: plannerKeys.config,
    queryFn: getPlannerConfig,
    refetchInterval: 30_000,
    staleTime: 10_000
  });
}

export function usePlannerTemplates(limit = 100) {
  return useQuery({
    queryKey: plannerKeys.templates(limit),
    queryFn: () => listPlannerTemplates(limit),
    staleTime: 60_000,
    refetchInterval: false,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 2;
    }
  });
}

export function usePlannerStatus() {
  return useQuery({ queryKey: plannerKeys.status, queryFn: () => getPlannerStatus(300), refetchInterval: 10_000 });
}

export function usePatchPlannerConfigMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PlannerConfigPatchInput) => patchPlannerConfig(input),
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}

export function useResetPlannerConfigMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: resetPlannerConfig,
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}

export function useRunPlannerOnceMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: runPlannerOnce,
    onSuccess: () => {
      invalidatePlannerQueries(queryClient);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    }
  });
}

export function useCreatePlannerTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: PlannerTemplateCreateInput) => createPlannerTemplate(input),
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}

export function usePatchPlannerTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ templateId, patch }: { templateId: string; patch: Record<string, unknown> }) => patchPlannerTemplate(templateId, patch),
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}

export function useDeletePlannerTemplateMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (templateId: string) => deletePlannerTemplate(templateId),
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}

export function useSaveRtxPresetMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: saveRtx5090Preset,
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}

export function useSaveJobsPresetMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: saveJobsPreset,
    onSuccess: () => invalidatePlannerQueries(queryClient)
  });
}
