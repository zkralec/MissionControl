import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createApplicationDraft,
  getApplicationDraftSummaries,
  reviewApplicationDraft,
  type CreateApplicationDraftInput,
  type DraftSummaryJobInput,
  type ReviewApplicationDraftInput,
} from "@/features/applications/api";

export const applicationDraftQueryKeys = {
  summary: (jobs: DraftSummaryJobInput[]) => ["application-drafts", "summary", jobs] as const,
};

export function useApplicationDraftSummaries(jobs: DraftSummaryJobInput[], enabled = true) {
  return useQuery({
    queryKey: applicationDraftQueryKeys.summary(jobs),
    queryFn: () => getApplicationDraftSummaries(jobs),
    enabled: enabled && jobs.length > 0,
    refetchInterval: 5_000,
  });
}

export function useCreateApplicationDraftMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateApplicationDraftInput) => createApplicationDraft(input),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["application-drafts"] });
    }
  });
}

export function useReviewApplicationDraftMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, input }: { taskId: string; input: ReviewApplicationDraftInput }) => reviewApplicationDraft(taskId, input),
    onSuccess: (_result, variables) => {
      queryClient.invalidateQueries({ queryKey: ["application-drafts"] });
      queryClient.invalidateQueries({ queryKey: ["task-result", variables.taskId] });
      queryClient.invalidateQueries({ queryKey: ["task", variables.taskId] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    }
  });
}
