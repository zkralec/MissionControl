import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(__dirname, "..");
const repoRoot = path.resolve(frontendDir, "..");
const tempSchema = path.resolve(frontendDir, "openapi.schema.json");
const outFile = path.resolve(frontendDir, "src/lib/api/generated/openapi.ts");

execFileSync(
  "python3",
  [
    "-c",
    [
      "import json",
      "import sys",
      "sys.path.insert(0, 'api')",
      "from main import app",
      `open(${JSON.stringify(tempSchema)}, 'w', encoding='utf-8').write(json.dumps(app.openapi(), indent=2))`
    ].join("; ")
  ],
  { stdio: "inherit", cwd: repoRoot }
);

execFileSync("npx", ["openapi-typescript", tempSchema, "--output", outFile], {
  stdio: "inherit",
  cwd: frontendDir
});

execFileSync("rm", ["-f", tempSchema], { stdio: "inherit", cwd: frontendDir });
