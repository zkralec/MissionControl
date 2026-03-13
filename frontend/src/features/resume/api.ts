import { apiRequest } from "@/lib/api/client";
import type { ResumeProfileOut } from "@/lib/api/generated/openapi";

export function getResumeProfile(includeText = false): Promise<ResumeProfileOut> {
  return apiRequest<ResumeProfileOut>(`/profile/resume?include_text=${includeText ? "true" : "false"}`);
}

export function saveResumeProfile(input: { resume_text: string; resume_name?: string | null }): Promise<ResumeProfileOut> {
  return apiRequest<ResumeProfileOut>("/profile/resume", {
    method: "PUT",
    body: input
  });
}

export function deleteResumeProfile(): Promise<{ deleted: boolean }> {
  return apiRequest<{ deleted: boolean }>("/profile/resume", { method: "DELETE" });
}

export function uploadResumeFile(file: File): Promise<ResumeProfileOut> {
  const formData = new FormData();
  formData.append("file", file, file.name || "resume");
  return apiRequest<ResumeProfileOut>("/profile/resume/upload", { method: "POST", body: formData });
}
