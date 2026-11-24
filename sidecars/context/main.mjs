// Sidecar backend for the Context Usage panel.
//
// Serves the panel and /api/report + /api/task-log endpoints backed by
// context_report.py and the live Antigravity model catalog.

import { existsSync, readFileSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Response, SidecarApp } from 'sidecar_sdk';
import {
  MAX_CATALOG_BYTES,
  UUID,
  collectDependencyStamps,
  dependencyStampsMatch,
  discoverWindowsPythonCandidates,
  execAsync,
  fileStamp,
  isLoopbackHostname,
  normalizeCatalogResponse,
  normalizeLsHostname,
  probePython,
  queryWindowsRegistryPython,
  readTaskLogSafe,
  resolveAppPaths,
  snapshotDependencyStamps,
  stampsUnchanged,
  workspacePath,
  writeJsonAtomic,
} from './sidecar_utils.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, 'context_report.py');
const APP_PATHS = resolveAppPaths();
const GEMINI_DIR = APP_PATHS.geminiDir;
const APP_DATA_DIR = APP_PATHS.appDataDir;
const APP_DIR_NAME = APP_PATHS.appDirName;
const CONVERSATIONS = join(APP_DATA_DIR, 'conversations');
const DATA_DIR = process.env.ANTIGRAVITY_EXECUTABLE_DATA_DIR || join(HERE, '.data');
const CATALOG_PATH = join(DATA_DIR, 'model-catalog.json');
const CATALOG_TTL_MS = 10 * 60 * 1000;
const CATALOG_RETRY_MS = 30 * 1000;
const MAX_CACHE_ENTRIES = 20;
const MAX_CACHE_AGE_MS = 60 * 1000;

const app = new SidecarApp();

// --- python -------------------------------------------------------------------

function buildPythonCandidates() {
  if (process.platform === 'win32') {
    return discoverWindowsPythonCandidates();
  }
  const list = [];
  if (process.env.ANTIGRAVITY_PYTHON) {
    list.push([process.env.ANTIGRAVITY_PYTHON.trim(), []]);
  }
  list.push(['python3', []], ['python', []]);
  return list;
}

let pythonPromise = null;

function findPython() {
  if (!pythonPromise) {
    const extra = process.platform === 'win32'
      ? () => queryWindowsRegistryPython(execAsync)
      : null;
    pythonPromise = probePython(buildPythonCandidates(), execAsync, extra).catch((err) => {
      pythonPromise = null;
      throw err;
    });
  }
  return pythonPromise;
}

// --- live model catalog -------------------------------------------------------

let catalogFetchedAt = 0;
let catalogRetryAfter = 0;
let catalogRefreshPromise = null;

function lsRequest(method, body) {
  const addr = (process.env.ANTIGRAVITY_LS_ADDRESS || '').trim();
  const csrf = (process.env.ANTIGRAVITY_CSRF_TOKEN || '').trim();
  if (!addr || !csrf) return Promise.reject(new Error('no language server address'));
  const base = new URL(addr.startsWith('http') ? addr : `http://${addr}`);
  const hostname = normalizeLsHostname(base.hostname);
  const data = JSON.stringify(body);
  const transport = base.protocol === 'https:' ? https : http;
  const loopback = isLoopbackHostname(hostname);

  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, val) => {
      if (settled) return;
      settled = true;
      clearTimeout(wallTimer);
      fn(val);
    };
    const req = transport.request(
      {
        hostname,
        port: base.port,
        path: `/exa.language_server_pb.LanguageServerService/${method}`,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Connect-Protocol-Version': '1',
          'x-codeium-csrf-token': csrf,
          'Content-Length': Buffer.byteLength(data),
        },
        timeout: 5000,
        autoSelectFamily: true,
        rejectUnauthorized: !loopback,
      },
      (res) => {
        const chunks = [];
        let totalBytes = 0;
        res.on('data', (chunk) => {
          totalBytes += chunk.length;
          if (totalBytes > MAX_CATALOG_BYTES) {
            req.destroy(new Error(`${method}: response exceeds ${MAX_CATALOG_BYTES} bytes`));
            return;
          }
          chunks.push(chunk);
        });
        res.on('error', (err) => finish(reject, err));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          if (res.statusCode < 200 || res.statusCode >= 300) {
            finish(reject, new Error(`${method}: HTTP ${res.statusCode}`));
            return;
          }
          try {
            finish(resolve, JSON.parse(text));
          } catch (e) {
            finish(reject, e);
          }
        });
      },
    );
    const wallTimer = setTimeout(() => {
      req.destroy(new Error(`${method}: deadline exceeded`));
    }, 8000);
    req.on('timeout', () => req.destroy(new Error(`${method}: timeout`)));
    req.on('error', (err) => finish(reject, err));
    req.end(data);
  });
}

async function refreshCatalog() {
  const now = Date.now();
  if (now < catalogRetryAfter) return;
  if (now - catalogFetchedAt < CATALOG_TTL_MS && existsSync(CATALOG_PATH)) return;
  if (catalogRefreshPromise) return catalogRefreshPromise;

  catalogRefreshPromise = (async () => {
    try {
      const resp = await lsRequest('GetAvailableModels', {
        metadata: { ideName: 'antigravity', extensionName: 'antigravity' },
      });
      const catalog = normalizeCatalogResponse(resp);
      if (Object.keys(catalog).length) {
        writeJsonAtomic(CATALOG_PATH, catalog);
        catalogFetchedAt = Date.now();
        catalogRetryAfter = 0;
      } else {
        catalogRetryAfter = Date.now() + CATALOG_RETRY_MS;
      }
    } catch (err) {
      catalogRetryAfter = Date.now() + CATALOG_RETRY_MS;
      console.error('[status-bar] model catalog unavailable, using built-in/cached limits:', err.message);
    } finally {
      catalogRefreshPromise = null;
    }
  })();

  return catalogRefreshPromise;
}

// --- report -------------------------------------------------------------------

const cache = new Map(); // conversationId -> { key, depStamps, createdAt, report }
const inFlight = new Map(); // `${conversationId}|${key}` -> Promise<object>

function setBoundedCache(conversationId, entry) {
  if (cache.has(conversationId)) cache.delete(conversationId);
  cache.set(conversationId, entry);
  while (cache.size > MAX_CACHE_ENTRIES) {
    const oldestKey = cache.keys().next().value;
    cache.delete(oldestKey);
  }
}

async function report(conversationId, workspaceUri) {
  if (typeof conversationId !== 'string' || !UUID.test(conversationId)) {
    return { error: 'no-conversation' };
  }
  const db = join(CONVERSATIONS, `${conversationId}.db`);
  if (!existsSync(db)) {
    return { error: 'no-database' };
  }
  await refreshCatalog();
  const workspace = workspacePath(workspaceUri);
  const tasksDir = join(APP_DATA_DIR, 'brain', conversationId, '.system_generated', 'tasks');
  const dbStamp = fileStamp(db);
  const walStamp = fileStamp(`${db}-wal`);
  const scriptStamp = fileStamp(SCRIPT);
  const catalogStamp = fileStamp(CATALOG_PATH);
  const tasksStamp = fileStamp(tasksDir);
  const key = JSON.stringify([dbStamp, walStamp, scriptStamp, workspace, catalogStamp, tasksStamp]);

  const cached = cache.get(conversationId);
  if (
    cached
    && cached.key === key
    && Date.now() - cached.createdAt < MAX_CACHE_AGE_MS
    && dependencyStampsMatch(cached.depStamps)
  ) {
    return cached.report;
  }

  const flightKey = `${conversationId}|${key}`;
  if (inFlight.has(flightKey)) {
    return inFlight.get(flightKey);
  }

  const job = (async () => {
    const runStartedMs = Date.now();
    const baseStamps = cached
      ? snapshotDependencyStamps(cached.depStamps)
      : collectDependencyStamps(null, CONVERSATIONS, tasksDir, GEMINI_DIR, workspace, APP_DIR_NAME);
    const { cmd, pre } = await findPython();
    const args = [...pre, SCRIPT, '--db', db, '--json', '--gemini-dir', GEMINI_DIR];
    if (APP_DIR_NAME !== 'antigravity') args.push('--app-dir', APP_DIR_NAME);
    if (existsSync(CATALOG_PATH)) args.push('--model-catalog', CATALOG_PATH);
    if (workspace) args.push('--workspace', workspace);
    let out;
    try {
      out = await execAsync(cmd, args);
    } catch (err) {
      if (err && /ENOENT|EACCES|EPERM|spawn/i.test(String(err.message || ''))) {
        pythonPromise = null;
      }
      throw err;
    }
    const parsed = JSON.parse(out);
    const result = { ...parsed, updatedAt: new Date().toISOString() };
    const depStamps = collectDependencyStamps(parsed, CONVERSATIONS, tasksDir, GEMINI_DIR, workspace, APP_DIR_NAME);
    const costComplete = !parsed?.cost_stats?.incomplete;
    const inputsStillFresh = fileStamp(db) === dbStamp
      && fileStamp(`${db}-wal`) === walStamp
      && fileStamp(CATALOG_PATH) === catalogStamp
      && stampsUnchanged(baseStamps, depStamps, runStartedMs);
    if (inputsStillFresh && costComplete) {
      setBoundedCache(conversationId, { key, depStamps, createdAt: Date.now(), report: result });
    }
    return result;
  })().finally(() => {
    inFlight.delete(flightKey);
  });

  inFlight.set(flightKey, job);
  return job;
}

// --- routes -------------------------------------------------------------------

app.page('/', () => new Response(readFileSync(join(HERE, 'index.html'), 'utf8'), { contentType: 'text/html' }));

app.api('/api/report', async (data) => {
  try {
    return await report(data.conversationId, data.workspace);
  } catch (err) {
    return { error: 'failed', message: err.message };
  }
}, 'POST');

app.api('/api/task-log', async (data) => {
  try {
    const cid = data?.conversationId;
    const stepNum = typeof data?.stepIdx === 'number'
      ? data.stepIdx
      : (typeof data?.stepIdx === 'string' && /^\d+$/.test(data.stepIdx) ? Number(data.stepIdx) : NaN);
    if (
      typeof cid !== 'string'
      || !UUID.test(cid)
      || !Number.isInteger(stepNum)
      || stepNum < 0
      || stepNum > 1_000_000
    ) {
      return { error: 'invalid-params' };
    }
    // Derive any forked task owner strictly server-side from the conversation's own DB report;
    // never trust a client-supplied taskId to read another conversation's log.
    let rep = cache.get(cid)?.report || null;
    if (!rep) {
      const fresh = await report(cid, '');
      if (fresh && !fresh.error) rep = fresh;
    }
    const tasks = Array.isArray(rep?.background_tasks) ? rep.background_tasks : [];
    const matched = tasks.find((t) => t.step_idx === stepNum && t.tool !== 'invoke_subagent');
    const trustedTaskId = matched && typeof matched.task_id === 'string' ? matched.task_id : '';
    return readTaskLogSafe(GEMINI_DIR, cid, stepNum, {
      taskId: trustedTaskId,
      appDirName: APP_DIR_NAME,
    });
  } catch (err) {
    return { error: 'failed', message: err.message };
  }
}, 'POST');

app.run();
refreshCatalog();
