import { useState } from "react";
import { ExternalLink } from "lucide-react";
import { useApiRuntime } from "@/app/providers";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorPanel } from "@/components/common/error-panel";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PageHeader } from "@/components/common/page-header";
import { SectionHeader } from "@/components/common/section-header";
import { ResumeProfileCard } from "@/features/resume/components";
import { useDeleteResumeMutation, useResumeProfile, useSaveResumeMutation, useUploadResumeMutation } from "@/features/resume/queries";
import { errorMessage } from "@/lib/utils/errors";

export function SettingsPage(): JSX.Element {
  const { apiKey, setApiKey } = useApiRuntime();
  const [showKey, setShowKey] = useState(false);

  const resumeQuery = useResumeProfile(true);
  const saveResume = useSaveResumeMutation();
  const deleteResume = useDeleteResumeMutation();
  const uploadResume = useUploadResumeMutation();
  const resumeError = [resumeQuery.error, saveResume.error, deleteResume.error, uploadResume.error].find(Boolean);

  return (
    <div className="space-y-4">
      <PageHeader title="Settings" subtitle="Operator runtime controls, credentials, and profile context." />
      {resumeError ? (
        <ErrorPanel
          title="Resume profile action failed"
          message={errorMessage(resumeError)}
          onAction={() => void resumeQuery.refetch()}
        />
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Runtime API Access</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <SectionHeader title="API Key" subtitle="Stored in runtime memory only. Not persisted to local storage." />
          <div className="grid gap-2 md:grid-cols-[1fr_auto_auto]">
            <div>
              <Label htmlFor="api-key">X-API-Key</Label>
              <Input
                id="api-key"
                type={showKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder="Enter API key"
              />
            </div>
            <Button variant="secondary" className="self-end" onClick={() => setShowKey((v) => !v)}>{showKey ? "Hide" : "Show"}</Button>
            <Button variant="outline" className="self-end" onClick={() => setApiKey("")}>Clear</Button>
          </div>
        </CardContent>
      </Card>

      <ResumeProfileCard
        profile={resumeQuery.data}
        onSave={(input) => saveResume.mutate(input)}
        onDelete={() => deleteResume.mutate()}
        onUpload={(file) => uploadResume.mutate(file)}
        busy={saveResume.isPending || deleteResume.isPending || uploadResume.isPending}
        loading={resumeQuery.isLoading}
      />

      <Card>
        <CardHeader>
          <CardTitle>Advanced Controls</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <details>
            <summary className="cursor-pointer font-medium">Developer links</summary>
            <div className="mt-3">
              <SectionHeader title="Platform Links" subtitle="Quick access to API docs and health endpoints." />
              <div className="flex flex-wrap gap-2">
                <a className="inline-flex items-center gap-1 rounded border border-border px-3 py-2 hover:bg-muted" href="/docs" target="_blank" rel="noreferrer">
                  Swagger <ExternalLink className="h-3.5 w-3.5" />
                </a>
                <a className="inline-flex items-center gap-1 rounded border border-border px-3 py-2 hover:bg-muted" href="/redoc" target="_blank" rel="noreferrer">
                  ReDoc <ExternalLink className="h-3.5 w-3.5" />
                </a>
                <a className="inline-flex items-center gap-1 rounded border border-border px-3 py-2 hover:bg-muted" href="/health" target="_blank" rel="noreferrer">
                  /health <ExternalLink className="h-3.5 w-3.5" />
                </a>
                <a className="inline-flex items-center gap-1 rounded border border-border px-3 py-2 hover:bg-muted" href="/ready" target="_blank" rel="noreferrer">
                  /ready <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            </div>
          </details>
        </CardContent>
      </Card>
    </div>
  );
}
