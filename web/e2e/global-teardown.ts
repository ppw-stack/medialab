import { readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { kill } from "node:process";

const SHARED_FILE = join(tmpdir(), "sh-e2e-shared.json");

export default async function globalTeardown() {
  if (!existsSync(SHARED_FILE)) return;
  const data = JSON.parse(readFileSync(SHARED_FILE, "utf-8"));
  for (const pid of [data.backendPid, data.frontendPid]) {
    if (pid) {
      try { kill(pid, "SIGTERM"); } catch {}
    }
  }
  await new Promise((r) => setTimeout(r, 1_000));
  for (const pid of [data.backendPid, data.frontendPid]) {
    if (pid) {
      try { kill(pid, "SIGKILL"); } catch {}
    }
  }
  if (data.workDir) {
    try { rmSync(data.workDir, { recursive: true, force: true }); } catch {}
  }
  try { rmSync(SHARED_FILE, { force: true }); } catch {}
}
