import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { ResumeProfileOut } from "@/lib/api/generated/openapi";
import { formatIso, formatInt } from "@/lib/utils/format";

export function ResumeProfileCard({
  profile,
  onSave,
  onDelete,
  onUpload,
  busy,
  loading
}: {
  profile: ResumeProfileOut | undefined;
  onSave: (input: { resume_text: string; resume_name?: string | null }) => void;
  onDelete: () => void;
  onUpload: (file: File) => void;
  busy?: boolean;
  loading?: boolean;
}): JSX.Element {
  const [resumeName, setResumeName] = useState(profile?.resume_name || "");
  const [resumeText, setResumeText] = useState(profile?.resume_text || "");

  useEffect(() => {
    setResumeName(profile?.resume_name || "");
    setResumeText(profile?.resume_text || "");
  }, [profile?.resume_name, profile?.resume_text, profile?.updated_at]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Resume Profile</CardTitle>
        <CardDescription>Stored profile is reused by jobs digest tasks.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {loading ? <div className="text-sm text-muted-foreground">Loading resume profile…</div> : null}
        <div className="rounded border border-border p-3 text-sm">
          <div>Loaded: {profile?.has_resume ? "Yes" : "No"}</div>
          <div>Name: {profile?.resume_name || "-"}</div>
          <div>Characters: {formatInt(profile?.resume_char_count || 0)}</div>
          <div>Updated: {formatIso(profile?.updated_at || null)}</div>
        </div>

        <div className="space-y-1">
          <Label htmlFor="resume-file">Upload file</Label>
          <Input
            id="resume-file"
            type="file"
            accept=".pdf,.docx,.txt,.md,.rtf,.json,.yaml,.yml,.log,.csv"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onUpload(file);
            }}
          />
        </div>

        <div className="space-y-1">
          <Label htmlFor="resume-name">Resume name</Label>
          <Input id="resume-name" value={resumeName} onChange={(e) => setResumeName(e.target.value)} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="resume-text">Resume text</Label>
          <Textarea id="resume-text" className="min-h-[180px]" value={resumeText} onChange={(e) => setResumeText(e.target.value)} />
        </div>

        <div className="flex flex-wrap gap-2">
          <Button onClick={() => onSave({ resume_name: resumeName || null, resume_text: resumeText })} disabled={busy || !resumeText.trim()}>
            Save Resume
          </Button>
          <Button variant="destructive" onClick={onDelete} disabled={busy}>
            Delete Resume
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
