import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const script = resolve(root, "scripts", "build-backend-sidecar.py");
const candidates = process.platform === "win32"
  ? [["python", []], ["python3", []], ["py", ["-3"]]]
  : [["python3", []], ["python", []]];

for (const [bin, prefixArgs] of candidates) {
  const probe = spawnSync(bin, [...prefixArgs, "--version"], { stdio: "ignore" });
  if (probe.status !== 0) continue;
  const result = spawnSync(bin, [...prefixArgs, script], { cwd: root, stdio: "inherit" });
  process.exit(result.status ?? 1);
}

console.error("Could not find Python. Install Python 3 and try again.");
process.exit(1);
