/**
 * @file src/web/indexJobs.ts
 * @brief Local web UI job runner for repository indexing requests.
 */

import { randomUUID } from "node:crypto";
import { existsSync, statSync } from "node:fs";
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { hostname } from "node:os";
import { basename, dirname, isAbsolute, join, resolve, win32 } from "node:path";
import { fileURLToPath } from "node:url";

const MAX_LOG_LINES = 1000;
const DEFAULT_CONTAINER_REPO_ROOT = "/workspace";
const DEFAULT_INDEXER_IMAGE = "codebrain-indexer:latest";
const DEFAULT_CLASSIFIER_BASE_URL = "http://classifier_proxy:3000";
const TERMINAL_ENV_ARGS = [
  "-e",
  "COLUMNS=120",
  "-e",
  "TERM=xterm-256color",
  "-e",
  "FORCE_COLOR=1",
  "-e",
  "TTY_COMPATIBLE=1",
  "-e",
  "TTY_INTERACTIVE=1",
  "-e",
  "PYTHONUNBUFFERED=1",
];

type IndexJobStatus = "running" | "completed" | "failed" | "cancelled";
type IndexJobLogStream = "stdout" | "stderr" | "system";

interface IndexJob {
  id: string;
  repo: string;
  repoPath: string;
  status: IndexJobStatus;
  startedAt: string;
  finishedAt: string | null;
  exitCode: number | null;
  logs: string[];
  lineBuffers: Record<IndexJobLogStream, string>;
  activeLineIndexes: Record<IndexJobLogStream, number | null>;
  child: ChildProcessWithoutNullStreams | null;
}

export interface IndexJobSnapshot {
  id: string;
  repo: string;
  repo_path: string;
  status: IndexJobStatus;
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  logs: string[];
}

const jobs = new Map<string, IndexJob>();

/**
 * @brief Resolve the repository root that owns docker/docker-compose.yml.
 * @returns Absolute CodeBrain repository root path.
 * @throws Error when the root cannot be found from the compiled module path.
 */
function resolveCodeBrainRoot(): string {
  const configuredRoot = process.env.CODEBRAIN_REPO_ROOT?.trim();
  if (configuredRoot && existsSync(join(configuredRoot, "docker", "docker-compose.yml"))) {
    return configuredRoot;
  }
  if (existsSync(join(DEFAULT_CONTAINER_REPO_ROOT, "docker", "docker-compose.yml"))) {
    return DEFAULT_CONTAINER_REPO_ROOT;
  }
  let current = dirname(fileURLToPath(import.meta.url));
  while (current !== dirname(current)) {
    if (existsSync(join(current, "docker", "docker-compose.yml"))) {
      return current;
    }
    current = dirname(current);
  }
  throw new Error("Could not locate CodeBrain repository root.");
}

/**
 * @brief Return whether this process is running inside a Linux container.
 * @returns True when a common container marker exists.
 */
function isContainerRuntime(): boolean {
  return existsSync("/.dockerenv");
}

/**
 * @brief Return whether a path looks absolute on Unix or Windows hosts.
 * @param repoPath Path string supplied by the browser.
 * @returns True when the path is absolute for a supported host platform.
 */
function isHostAbsolutePath(repoPath: string): boolean {
  return isAbsolute(repoPath) || win32.isAbsolute(repoPath);
}

/**
 * @brief Extract a folder basename from a platform or Windows-style path.
 * @param repoPath Repository path supplied by the browser.
 * @returns Final folder name.
 */
function getRepoPathBasename(repoPath: string): string {
  const trimmed = repoPath.trim().replace(/[\\/]+$/, "");
  if (trimmed.includes("\\")) {
    return win32.basename(trimmed);
  }
  return basename(trimmed);
}

/**
 * @brief Validate the requested repository path before starting Docker.
 * @param repo Repository name selected in the web UI.
 * @param repoPath Absolute local repository path.
 * @returns Normalized absolute repository path.
 * @throws Error when the path is invalid or does not match the selected repo.
 */
function validateRepoPath(repo: string, repoPath: string): string {
  const normalizedPath = repoPath.trim();
  if (!normalizedPath) {
    throw new Error("Repository path is required.");
  }
  if (!isHostAbsolutePath(normalizedPath)) {
    throw new Error("Repository path must be absolute.");
  }
  const pathForDocker = win32.isAbsolute(normalizedPath) ? normalizedPath : resolve(normalizedPath);
  const folderName = getRepoPathBasename(pathForDocker);
  if (folderName !== repo) {
    throw new Error(`Selected folder "${folderName}" does not match repository "${repo}".`);
  }
  if (existsSync(pathForDocker)) {
    if (!statSync(pathForDocker).isDirectory()) {
      throw new Error(`Repository path is not a directory: ${pathForDocker}`);
    }
    return pathForDocker;
  }
  if (isContainerRuntime()) {
    return pathForDocker;
  }
  throw new Error(`Repository path does not exist: ${pathForDocker}`);
}

/**
 * @brief Parse KEY=VALUE lines emitted by the container endpoint resolver.
 * @param output Resolver stdout text.
 * @returns Environment key/value additions.
 */
function parseEnvironmentLines(output: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const line of output.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const equalsIndex = trimmed.indexOf("=");
    if (equalsIndex <= 0) continue;
    const key = trimmed.slice(0, equalsIndex);
    const value = trimmed.slice(equalsIndex + 1);
    env[key] = value;
  }
  return env;
}

/**
 * @brief Resolve container-safe endpoint environment variables for indexing.
 * @param repoRoot Absolute CodeBrain repository root.
 * @returns Environment additions for the Docker Compose run.
 * @throws Error when no Python launcher can run the resolver successfully.
 */
function resolveContainerEndpointEnvironment(repoRoot: string): Record<string, string> {
  const resolverScript = join(repoRoot, "scripts", "resolve-container-endpoints.py");
  const candidates: { command: string; args: string[] }[] = [
    { command: "python3", args: [resolverScript, repoRoot] },
    { command: "python", args: [resolverScript, repoRoot] },
    { command: "py", args: ["-3", resolverScript, repoRoot] },
  ];

  const errors: string[] = [];
  for (const candidate of candidates) {
    const result = spawnSync(candidate.command, candidate.args, {
      cwd: repoRoot,
      encoding: "utf8",
      env: process.env,
    });
    if (result.status === 0) {
      return parseEnvironmentLines(String(result.stdout || ""));
    }
    const stderr = String(result.stderr || "").trim();
    const stdout = String(result.stdout || "").trim();
    errors.push(`${candidate.command}: ${stderr || stdout || result.error?.message || "failed"}`);
  }
  throw new Error(`Failed to resolve container endpoints. ${errors.join(" | ")}`);
}

interface DockerContainerMount {
  Destination?: string;
  Source?: string;
}

interface DockerContainerInspect {
  Mounts?: DockerContainerMount[];
  NetworkSettings?: {
    Networks?: Record<string, unknown>;
  };
}

interface ContainerDockerContext {
  hostRepoRoot: string;
  networkName: string;
}

/**
 * @brief Inspect the current container through the host Docker socket.
 * @returns Docker inspect payload for the current container.
 * @throws Error when the current container cannot be inspected.
 */
function inspectCurrentContainer(): DockerContainerInspect {
  const result = spawnSync("docker", ["inspect", hostname()], {
    encoding: "utf8",
    env: process.env,
  });
  if (result.status !== 0) {
    const detail = String(result.stderr || result.stdout || result.error?.message || "unknown error").trim();
    throw new Error(`Failed to inspect the web server container through Docker: ${detail}`);
  }
  const parsed = JSON.parse(String(result.stdout || "[]")) as DockerContainerInspect[];
  const inspect = parsed[0];
  if (!inspect) {
    throw new Error("Docker inspect returned no current-container metadata.");
  }
  return inspect;
}

/**
 * @brief Resolve host-side Docker context needed for sibling indexer runs.
 * @returns Host repository mount source and active Docker network name.
 */
function resolveContainerDockerContext(): ContainerDockerContext {
  const inspect = inspectCurrentContainer();
  const workspaceMount = (inspect.Mounts || []).find((mount) => mount.Destination === DEFAULT_CONTAINER_REPO_ROOT);
  const hostRepoRoot = workspaceMount?.Source;
  if (!hostRepoRoot) {
    throw new Error("The web server container is missing a /workspace host bind mount.");
  }
  const networkNames = Object.keys(inspect.NetworkSettings?.Networks || {});
  const networkName = networkNames.find((name) => name.endsWith("_codebrain_internal"))
    || networkNames.find((name) => name.includes("codebrain_internal"))
    || networkNames[0];
  if (!networkName) {
    throw new Error("The web server container is not attached to a Docker network.");
  }
  return { hostRepoRoot, networkName };
}

/**
 * @brief Build Docker arguments for a sibling indexer run from inside Docker.
 * @param repo Repository name selected in the UI.
 * @param targetPath Host path to mount at `/target`.
 * @param workerCount Worker count to pass to ingestion.
 * @returns Docker CLI arguments for `docker run`.
 */
function buildContainerDockerArgs(repo: string, targetPath: string, workerCount: number): string[] {
  const context = resolveContainerDockerContext();
  return [
    "run",
    "--rm",
    "--network",
    context.networkName,
    "-v",
    `${context.hostRepoRoot}:/workspace`,
    "-v",
    `${targetPath}:/target`,
    "-w",
    "/workspace",
    "-e",
    `DATABASE_URL=${process.env.DATABASE_URL || "postgresql://codebrain:codebrain_local@postgres:5432/codebrain"}`,
    "-e",
    `EMBED_BASE_URL=${process.env.EMBED_BASE_URL || "http://embed_proxy:11434"}`,
    "-e",
    `CLASSIFIER_BASE_URL=${process.env.CLASSIFIER_BASE_URL || DEFAULT_CLASSIFIER_BASE_URL}`,
    ...TERMINAL_ENV_ARGS,
    process.env.CODEBRAIN_INDEXER_IMAGE || DEFAULT_INDEXER_IMAGE,
    "python",
    "-m",
    "codebrain.ingest",
    "/target",
    "--repo-name",
    repo,
    "--workers",
    String(workerCount),
  ];
}

/**
 * @brief Build Docker Compose arguments for a host-local indexer run.
 * @param repoRoot Absolute CodeBrain repository root on the host.
 * @param repo Repository name selected in the UI.
 * @param targetPath Host path to mount at `/target`.
 * @param workerCount Worker count to pass to ingestion.
 * @returns Docker CLI arguments for `docker compose run`.
 */
function buildHostDockerArgs(repoRoot: string, repo: string, targetPath: string, workerCount: number): string[] {
  return [
    "compose",
    "-f",
    join(repoRoot, "docker", "docker-compose.yml"),
    "--profile",
    "indexer",
    "run",
    "--rm",
    ...TERMINAL_ENV_ARGS,
    "-v",
    `${targetPath}:/target`,
    "indexer",
    "python",
    "-m",
    "codebrain.ingest",
    "/target",
    "--repo-name",
    repo,
    "--workers",
    String(workerCount),
  ];
}

/**
 * @brief Convert an internal job record into a JSON-safe response shape.
 * @param job Internal job record.
 * @returns Public job snapshot.
 */
function snapshotJob(job: IndexJob): IndexJobSnapshot {
  return {
    id: job.id,
    repo: job.repo,
    repo_path: job.repoPath,
    status: job.status,
    started_at: job.startedAt,
    finished_at: job.finishedAt,
    exit_code: job.exitCode,
    logs: [...job.logs],
  };
}

/** @brief Remove ANSI control sequences before storing terminal text. */
function stripAnsi(text: string): string {
  return text
    .replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "")
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "");
}

/**
 * @brief Trim retained terminal lines and keep mutable-line indexes valid.
 * @param job Index job whose logs should be bounded.
 */
function pruneLogLines(job: IndexJob): void {
  if (job.logs.length > MAX_LOG_LINES) {
    const removed = job.logs.length - MAX_LOG_LINES;
    job.logs.splice(0, removed);
    for (const stream of Object.keys(job.activeLineIndexes) as IndexJobLogStream[]) {
      const index = job.activeLineIndexes[stream];
      job.activeLineIndexes[stream] = index === null || index < removed ? null : index - removed;
    }
  }
}

/**
 * @brief Store an active terminal line, replacing the previous frame for CR updates.
 * @param job Index job receiving output.
 * @param stream Output stream label.
 * @param text Current terminal line text.
 */
function writeMutableLogLine(job: IndexJob, stream: IndexJobLogStream, text: string): void {
  const cleanLine = text.trimEnd();
  if (!cleanLine) return;
  const renderedLine = `[${stream}] ${cleanLine}`;
  const index = job.activeLineIndexes[stream];
  if (index !== null && index >= 0 && index < job.logs.length) {
    job.logs[index] = renderedLine;
  } else {
    job.logs.push(renderedLine);
    job.activeLineIndexes[stream] = job.logs.length - 1;
  }
  pruneLogLines(job);
}

/**
 * @brief Finalize a terminal line after a newline is received.
 * @param job Index job receiving output.
 * @param stream Output stream label.
 * @param text Completed terminal line text.
 */
function finalizeLogLine(job: IndexJob, stream: IndexJobLogStream, text: string): void {
  writeMutableLogLine(job, stream, text);
  job.activeLineIndexes[stream] = null;
}

/**
 * @brief Append terminal output while honoring carriage-return line rewrites.
 * @param job Index job receiving output.
 * @param stream Output stream label.
 * @param chunk Raw output bytes or text.
 */
function appendLogChunk(job: IndexJob, stream: IndexJobLogStream, chunk: Buffer | string): void {
  if (stream === "system") {
    for (const line of stripAnsi(String(chunk)).split(/\r\n|\r|\n/g)) {
      const cleanLine = line.trimEnd();
      if (!cleanLine) continue;
      job.logs.push(`[${stream}] ${cleanLine}`);
    }
    pruneLogLines(job);
    return;
  }

  const text = stripAnsi(String(chunk));
  let buffer = job.lineBuffers[stream] || "";
  for (const char of text) {
    if (char === "\r") {
      writeMutableLogLine(job, stream, buffer);
      buffer = "";
    } else if (char === "\n") {
      finalizeLogLine(job, stream, buffer);
      buffer = "";
    } else {
      buffer += char;
    }
  }
  job.lineBuffers[stream] = buffer;
  if (buffer) {
    writeMutableLogLine(job, stream, buffer);
  }
}

/**
 * @brief Return whether a repository already has a running index job.
 * @param repo Repository name.
 * @returns True when an active job exists.
 */
function hasRunningJobForRepo(repo: string): boolean {
  for (const job of jobs.values()) {
    if (job.repo === repo && job.status === "running") {
      return true;
    }
  }
  return false;
}

/**
 * @brief Start a Docker-backed index job for a repository path.
 * @param repo Repository name selected in the UI.
 * @param repoPath Absolute host path to the repository.
 * @param workers Worker count to pass to ingestion.
 * @returns Public snapshot for the new job.
 */
export function startIndexJob(repo: string, repoPath: string, workers = 2): IndexJobSnapshot {
  if (hasRunningJobForRepo(repo)) {
    throw new Error(`Repository "${repo}" is already being indexed.`);
  }

  const targetPath = validateRepoPath(repo, repoPath);
  const repoRoot = resolveCodeBrainRoot();
  const workerCount = Number.isFinite(workers) && workers > 0 ? Math.floor(workers) : 2;
  const containerRuntime = isContainerRuntime();
  const endpointEnv = containerRuntime ? {} : resolveContainerEndpointEnvironment(repoRoot);
  const args = containerRuntime
    ? buildContainerDockerArgs(repo, targetPath, workerCount)
    : buildHostDockerArgs(repoRoot, repo, targetPath, workerCount);

  const job: IndexJob = {
    id: randomUUID(),
    repo,
    repoPath: targetPath,
    status: "running",
    startedAt: new Date().toISOString(),
    finishedAt: null,
    exitCode: null,
    logs: [],
    lineBuffers: { stdout: "", stderr: "", system: "" },
    activeLineIndexes: { stdout: null, stderr: null, system: null },
    child: null,
  };
  appendLogChunk(job, "system", `docker ${args.join(" ")}`);

  const child = spawn("docker", args, {
    cwd: repoRoot,
    env: { ...process.env, ...endpointEnv },
  });
  job.child = child;
  jobs.set(job.id, job);

  child.stdout.on("data", (chunk) => appendLogChunk(job, "stdout", chunk));
  child.stderr.on("data", (chunk) => appendLogChunk(job, "stderr", chunk));
  child.on("error", (error) => {
    job.status = "failed";
    job.exitCode = null;
    job.finishedAt = new Date().toISOString();
    appendLogChunk(job, "system", error.message);
  });
  child.on("close", (code) => {
    if (job.status === "cancelled") {
      job.exitCode = code;
    } else if (code === 0) {
      job.status = "completed";
      job.exitCode = 0;
    } else {
      job.status = "failed";
      job.exitCode = code;
    }
    job.finishedAt = new Date().toISOString();
    appendLogChunk(job, "system", `Indexer exited with code ${code ?? "unknown"}.`);
    job.child = null;
  });

  return snapshotJob(job);
}

/**
 * @brief Fetch a previously started index job.
 * @param jobId Index job identifier.
 * @returns Public job snapshot, or null when missing.
 */
export function getIndexJobSnapshot(jobId: string): IndexJobSnapshot | null {
  const job = jobs.get(jobId);
  return job ? snapshotJob(job) : null;
}

/**
 * @brief Cancel a running index job.
 * @param jobId Index job identifier.
 * @returns Public job snapshot, or null when missing.
 */
export function cancelIndexJob(jobId: string): IndexJobSnapshot | null {
  const job = jobs.get(jobId);
  if (!job) {
    return null;
  }
  if (job.status !== "running") {
    return snapshotJob(job);
  }
  job.status = "cancelled";
  job.finishedAt = new Date().toISOString();
  appendLogChunk(job, "system", "Cancellation requested.");
  job.child?.kill("SIGTERM");
  return snapshotJob(job);
}
