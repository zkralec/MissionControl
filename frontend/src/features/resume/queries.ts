import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { deleteResumeProfile, getResumeProfile, saveResumeProfile, uploadResumeFile } from "@/features/resume/api";

const resumeKeys = {
  profile: ["resume", "profile"] as const
};

export function useResumeProfile(includeText = true) {
  return useQuery({ queryKey: resumeKeys.profile, queryFn: () => getResumeProfile(includeText), refetchInterval: 30_000 });
}

export function useSaveResumeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: saveResumeProfile,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["resume"] })
  });
}

export function useDeleteResumeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteResumeProfile,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["resume"] })
  });
}

export function useUploadResumeMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: uploadResumeFile,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["resume"] })
  });
}
