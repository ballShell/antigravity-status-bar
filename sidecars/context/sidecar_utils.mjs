import { execFile } from 'node:child_process';
import {
  closeSync,
  constants,
  existsSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const MAX_LOG_BYTES = 32 * 1024;
export const MAX_CATALOG_BYTES = 4 * 1024 * 1024;

export function isLoopbackHostname(hostname) {
  const h = String(hostname || '').toLowerCase();
  return h === 'localhost' || h === '127.0.0.1' || h === '::1' || h === '[::1]';
}

export function workspacePath(uri) {
  if (!uri || typeof uri !== 'string' || !uri.startsWith('file:')) return '';
  try {
    return fileURLToPath(uri);
  } catch {
    return '';
  }
}

export function fileStamp(path) {
  try {
    const s = statSync(path);
    return `${s.mtimeMs}:${s.size}`;
  } catch {
    return '-';
  }
}

export function execAsync(cmd, args, timeout = 20000) {
  return new Promise((resolve, reject) => {
    execFile(
      cmd,
      args,
      {
        timeout,
        maxBuffer: 16 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
        windowsHide: true,
      },
      (err, stdout, stderr) => {
        if (!err) {
          resolve(stdout);
          return;
        }
        if (err.killed || err.signal) {
          reject(new Error(`${cmd} timed out (${err.signal || 'killed'})`));
          return;
        }
        const detail = (stderr || err.message || '').trim();
        reject(new Error(detail || `${cmd} exited with code ${err.code ?? 'unknown'}`));
      },
    );
  });
}

export async function probePython(candidates, runner = execAsync) {
  const probeCode = 'import sys, sqlite3; sys.exit(0 if sys.version_info >= (3, 9) else 1)';
  const failures = [];
  for (const [cmd, pre] of candidates) {
    try {
      await runner(cmd, [...pre, '-c', probeCode], 5000);
      return { cmd, pre };
    } catch (err) {
      const label = [cmd, ...pre].join(' ');
      failures.push(`${label}: ${err.message}`);
    }
  }
  throw new Error(
    `Python 3.9+ with sqlite3 not found (${failures.join('; ') || 'no candidates'}). `
      + 'Install Python 3.9+ and restart Antigravity.',
  );
}

export function isPathStrictlyInside(realRoot, targetPath) {
  const rel = relative(realRoot, targetPath);
  return Boolean(rel) && !rel.startsWith('..') && !isAbsolute(rel);
}

function verifySegmentsNoSymlink(rootDir, segments, realRoot) {
  let cur = rootDir;
  let lastStat = null;
  for (let i = 0; i < segments.length; i++) {
    cur = join(cur, segments[i]);
    const lst = lstatSync(cur);
    if (lst.isSymbolicLink()) return null;
    const isLast = i === segments.length - 1;
    if (isLast ? !lst.isFile() : !lst.isDirectory()) return null;
    lastStat = lst;
  }
  const realTarget = realpathSync(cur);
  if (!isPathStrictlyInside(realRoot, realTarget)) return null;
  return { realTarget, leafStat: lastStat };
}

export function readTaskLogSafe(geminiDir, conversationId, stepIdx) {
  const idxStr = String(stepIdx ?? '');
  if (!UUID.test(conversationId || '') || !/^\d+$/.test(idxStr)) {
    return { error: 'invalid-params' };
  }
  const empty = { exists: false, content: '', size: 0, mtimeMs: 0, truncated: false };

  let realRoot;
  let preCheck;
  try {
    realRoot = realpathSync(geminiDir);
    const segments = ['antigravity', 'brain', conversationId, '.system_generated', 'tasks', `task-${idxStr}.log`];
    preCheck = verifySegmentsNoSymlink(geminiDir, segments, realRoot);
    if (!preCheck) return empty;
  } catch {
    return empty;
  }

  const flags =
    constants.O_RDONLY
    | (typeof constants.O_NOFOLLOW === 'number' ? constants.O_NOFOLLOW : 0)
    | (typeof constants.O_NONBLOCK === 'number' ? constants.O_NONBLOCK : 0);
  let fd;
  try {
    fd = openSync(preCheck.realTarget, flags);
  } catch (err) {
    if (err && (err.code === 'ENOENT' || err.code === 'ELOOP' || err.code === 'ENOTDIR' || err.code === 'ENXIO')) {
      return empty;
    }
    throw err;
  }

  try {
    const stat = fstatSync(fd);
    if (!stat.isFile()) return empty;
    // Post-open re-verification: confirm ancestors still contain no symlinks and the opened
    // file descriptor matches the exact resolved leaf file inside realRoot.
    const postRoot = realpathSync(geminiDir);
    const segments = ['antigravity', 'brain', conversationId, '.system_generated', 'tasks', `task-${idxStr}.log`];
    const postCheck = verifySegmentsNoSymlink(geminiDir, segments, postRoot);
    if (
      !postCheck
      || postCheck.leafStat.dev !== stat.dev
      || postCheck.leafStat.ino !== stat.ino
    ) {
      return empty;
    }
    const truncated = stat.size > MAX_LOG_BYTES;
    const toRead = Math.min(stat.size, MAX_LOG_BYTES);
    const offset = stat.size - toRead;
    let content = '';
    if (toRead > 0) {
      const buf = Buffer.allocUnsafe(toRead);
      let readTotal = 0;
      while (readTotal < toRead) {
        const n = readSync(fd, buf, readTotal, toRead - readTotal, offset + readTotal);
        if (n <= 0) break;
        readTotal += n;
      }
      content = buf.subarray(0, readTotal).toString('utf8');
      if (truncated) {
        const nl = content.indexOf('\n');
        if (nl !== -1 && nl < 256) content = content.slice(nl + 1);
      }
    }
    return {
      exists: true,
      content,
      size: stat.size,
      mtimeMs: stat.mtimeMs,
      truncated,
    };
  } finally {
    closeSync(fd);
  }
}

export function normalizeCatalogResponse(resp) {
  const models = resp?.response?.models || resp?.models;
  if (!models || typeof models !== 'object' || Array.isArray(models)) return {};
  const catalog = {};
  for (const [id, m] of Object.entries(models)) {
    if (!m || typeof m !== 'object') continue;
    const rawMax = Number(m.maxTokens);
    const maxTokens = Number.isFinite(rawMax) && rawMax > 0 ? Math.floor(rawMax) : 0;
    const displayName = typeof m.displayName === 'string' ? m.displayName : '';
    if (maxTokens > 0 || displayName) {
      catalog[id] = { maxTokens, displayName };
    }
  }
  return catalog;
}

export function writeJsonAtomic(filePath, data) {
  const dir = dirname(filePath);
  mkdirSync(dir, { recursive: true });
  const tmpPath = `${filePath}.tmp.${process.pid}.${Date.now()}`;
  try {
    writeFileSync(tmpPath, JSON.stringify(data), 'utf8');
    renameSync(tmpPath, filePath);
  } catch (err) {
    try {
      if (existsSync(tmpPath)) unlinkSync(tmpPath);
    } catch { /* ignore cleanup error */ }
    throw err;
  }
}

export function systemFileStampPaths(geminiDir, workspace) {
  const paths = [
    geminiDir,
    join(geminiDir, 'GEMINI.md'),
    join(geminiDir, 'antigravity'),
  ];
  if (workspace) {
    for (const root of ['.agents', '_agents']) {
      paths.push(join(workspace, root), join(workspace, root, 'hooks.json'));
    }
  }
  return paths;
}

export function collectDependencyStamps(reportData, conversationsDir, tasksDir, geminiDir = '', workspace = '') {
  const seenIdx = new Map();
  const stamps = [];
  const addPath = (p, expectExists = false) => {
    if (!p) return;
    if (seenIdx.has(p)) {
      if (expectExists) stamps[seenIdx.get(p)][2] = true;
      return;
    }
    seenIdx.set(p, stamps.length);
    stamps.push([p, fileStamp(p), Boolean(expectExists)]);
  };

  if (geminiDir) {
    for (const p of systemFileStampPaths(geminiDir, workspace)) addPath(p, false);
  }

  const tasks = Array.isArray(reportData?.background_tasks) ? reportData.background_tasks : [];
  for (const t of tasks) {
    if (t.tool === 'invoke_subagent' && typeof t.task_id === 'string' && UUID.test(t.task_id)) {
      const subDb = join(conversationsDir, `${t.task_id}.db`);
      addPath(subDb, t.status !== 'unknown');
      addPath(`${subDb}-wal`, false);
    } else if (t.tool !== 'invoke_subagent') {
      if (typeof t.log_path === 'string' && t.log_path) addPath(t.log_path, Boolean(t.has_log));
      if (tasksDir && Number.isInteger(t.step_idx)) addPath(join(tasksDir, `task-${t.step_idx}.log`), false);
    }
  }
  const extraSubIds = Array.isArray(reportData?.cost_stats?.subagent_conversation_ids)
    ? reportData.cost_stats.subagent_conversation_ids
    : [];
  for (const sid of extraSubIds) {
    if (typeof sid === 'string' && UUID.test(sid)) {
      const subDb = join(conversationsDir, `${sid}.db`);
      addPath(subDb, true);
      addPath(`${subDb}-wal`, false);
    }
  }
  return stamps;
}

export function snapshotDependencyStamps(stamps) {
  if (!Array.isArray(stamps)) return [];
  return stamps.map(([p, , expectExists]) => [p, fileStamp(p), Boolean(expectExists)]);
}

export function fileMtimeMs(path) {
  try {
    return statSync(path).mtimeMs;
  } catch {
    return 0;
  }
}

export function dependencyStampsMatch(stamps) {
  if (!Array.isArray(stamps)) return true;
  for (const [path, expected] of stamps) {
    if (fileStamp(path) !== expected) return false;
  }
  return true;
}

export function stampsUnchanged(beforeStamps, afterStamps, runStartedMs = 0) {
  const afterMap = new Map((afterStamps || []).map(([p, v]) => [p, v]));
  for (const [path, beforeVal] of beforeStamps || []) {
    if (afterMap.has(path) && afterMap.get(path) !== beforeVal) return false;
    if (!afterMap.has(path) && fileStamp(path) !== beforeVal) return false;
  }
  if (runStartedMs > 0) {
    const beforeSet = new Set((beforeStamps || []).map(([p]) => p));
    for (const [path, afterVal, expectExists] of afterStamps || []) {
      if (expectExists && afterVal === '-') return false;
      if (!beforeSet.has(path) && afterVal !== '-' && fileMtimeMs(path) >= runStartedMs - 50) return false;
    }
  }
  return true;
}
