import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api/client";
import {
  createWatcher,
  deleteWatcher,
  getWatcher,
  listWatchers,
  patchWatcher,
  type WatcherCreateInput,
  type WatcherPatchInput
} from "@/features/watchers/api";

export const watcherKeys = {
  all: ["watchers"] as const,
  list: (limit: number, enabledOnly: boolean) => ["watchers", "list", { limit, enabledOnly }] as const,
  detail: (watcherId: string) => ["watchers", "detail", watcherId] as const
};

function invalidateWatcherQueries(queryClient: ReturnType<typeof useQueryClient>): void {
  queryClient.invalidateQueries({ queryKey: watcherKeys.all });
  queryClient.invalidateQueries({ queryKey: ["planner"] });
  queryClient.invalidateQueries({ queryKey: ["tasks"] });
  queryClient.invalidateQueries({ queryKey: ["runs"] });
}

export function useWatchers(limit = 100, enabledOnly = false) {
  return useQuery({
    queryKey: watcherKeys.list(limit, enabledOnly),
    queryFn: () => listWatchers(limit, enabledOnly),
    staleTime: 30_000,
    refetchInterval: 12_000,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 401) return false;
      return failureCount < 2;
    }
  });
}

export function useWatcher(watcherId: string | null) {
  return useQuery({
    queryKey: watcherKeys.detail(watcherId || ""),
    queryFn: () => getWatcher(watcherId || ""),
    enabled: Boolean(watcherId),
    staleTime: 10_000,
    refetchInterval: 10_000
  });
}

export function useCreateWatcherMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: WatcherCreateInput) => createWatcher(input),
    onSuccess: () => invalidateWatcherQueries(queryClient)
  });
}

export function usePatchWatcherMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ watcherId, patch }: { watcherId: string; patch: WatcherPatchInput }) => patchWatcher(watcherId, patch),
    onSuccess: () => invalidateWatcherQueries(queryClient)
  });
}

export function useDeleteWatcherMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (watcherId: string) => deleteWatcher(watcherId),
    onSuccess: () => invalidateWatcherQueries(queryClient)
  });
}
