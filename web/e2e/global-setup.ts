/**
 * E2E 全局启动
 *
 * 1. 启动后端（uvicorn，端口 BACKEND_PORT）
 * 2. 启动前端（vite preview，端口 FRONTEND_PORT）
 * 3. 写共享 JSON（backend/frontend PIDs + 凭据）给 teardown
 */
import { spawn, type ChildProcess } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes, createHash } from "node:crypto";
import { BACKEND_PORT, FRONTEND_PORT } from "../playwright.config";

const SHARED_FILE = join(tmpdir(), "sh-e2e-shared.json");

/**
 * Node 26 的 pbkdf2Sync 与 Python 3.12 的 hashlib.pbkdf2_hmac 不兼容
 * （Node 26 在某些场景把 keylen 误读为 digest）。所以我们直接复刻
 * Python 端 hashlib.pbkdf2_hmac('sha256', ...) 的算法逻辑：
 *   T = U1 ^ U2 ^ ... ^ Uc, Ui = HMAC-SHA256(password, salt ++ i)
 * 这是 PBKDF2 标准，输出与 hashlib 完全一致。
 */
function pbkdf2HmacSha256Hex(
  password: string, salt: Buffer, iterations: number, keylen: number,
): string {
  const blocks = Math.ceil(keylen / 32);
  const result = Buffer.alloc(blocks * 32);
  for (let i = 1; i <= blocks; i++) {
    const block = Buffer.alloc(4);
    block.writeUInt32BE(i, 0);
    const u1 = createHash("sha256")
      .update(Buffer.concat([salt, block]))
      .update(Buffer.from(password, "utf-8"))
      .digest();
    u1.copy(result, (i - 1) * 32);
    for (let j = 1; j < iterations; j++) {
      const uNext = createHash("sha256").update(u1).digest();
      uNext.copy(u1, 0);
      for (let k = 0; k < 32; k++) {
        result[(i - 1) * 32 + k] ^= u1[k];
      }
    }
  }
  return result.subarray(0, keylen).toString("hex");
}

function hashPassword(password: string): string {
  const salt = randomBytes(16);
  const dk = pbkdf2HmacSha256Hex(password, salt, 120_000, 32);
  return `pbkdf2$120000$${salt.toString("hex")}$${dk}`;
}

function generateKey(): string {
  return randomBytes(32).toString("base64url");
}

export default async function globalSetup() {
  const workDir = mkdtempSync(join(tmpdir(), "sh-e2e-"));
  const dataDir = join(workDir, "data");
  const logDir = join(workDir, "logs");
  mkdirSync(dataDir, { recursive: true });
  mkdirSync(logDir, { recursive: true });

  const encryptionKey = generateKey();
  const password = "admin-password";
  const hashedPassword = hashPassword(password);

  const env = {
    ...process.env,
    ENCRYPTION_KEY: encryptionKey,
    WEB_PASSWORD: hashedPassword,
    WEB_TOKEN: "e2e-cli-token",
    DATA_DIR: dataDir,
    LOG_DIR: logDir,
    LOG_LEVEL: "WARNING",
    LOG_JSON: "false",
  };

  // 1. 启动后端
  const backend = spawn(
    "uv",
    [
      "run", "uvicorn", "app.main:app",
      "--host", "127.0.0.1",
      "--port", String(BACKEND_PORT),
      "--no-access-log",
      "--workers", "1",
    ],
    {
      env,
      cwd: join(import.meta.dirname, "../.."),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (!backend.pid) throw new Error("Failed to spawn backend");

  // 2. 启动前端 vite preview
  const distPath = join(import.meta.dirname, "../dist");
  if (!existsSync(distPath)) {
    throw new Error(`dist/ not found at ${distPath}. Run 'npm run build' first.`);
  }
  const frontend = spawn(
    "npx",
    ["vite", "preview", "--host", "127.0.0.1", "--port", String(FRONTEND_PORT), "--strictPort"],
    {
      cwd: join(import.meta.dirname, ".."),  // web/
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  if (!frontend.pid) throw new Error("Failed to spawn frontend");

  await waitFor(BACKEND_PORT, "/healthz", 30_000);
  await waitFor(FRONTEND_PORT, "/", 10_000);

  writeFileSync(
    SHARED_FILE,
    JSON.stringify({
      backendPid: backend.pid,
      frontendPid: frontend.pid,
      workDir,
      backendUrl: `http://127.0.0.1:${BACKEND_PORT}`,
      frontendUrl: `http://127.0.0.1:${FRONTEND_PORT}`,
      encryptionKey,
      password,
      hashedPassword,
      token: "e2e-cli-token",
    }),
  );
  process.env.SH_E2E_SHARED = SHARED_FILE;
  process.stderr.write(`[gs] shared=${SHARED_FILE}\n`);
}

async function waitFor(port: number, path: string, timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}${path}`);
      if (r.ok || r.status < 500) return;
    } catch {}
    await sleep(200);
  }
  throw new Error(`Service on ${port} did not become ready (${path})`);
}
