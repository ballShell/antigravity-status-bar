import { execFile } from 'node:child_process';
import {
  closeSync,
  constants,
  existsSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readlinkSync,
  readSync,
  realpathSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, isAbsolute, join, relative, sep, win32 as winPath } from 'node:path';
import { fileURLToPath } from 'node:url';

export const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const FORKED_TASK_ID_RE = /^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})[\\/]task-(\d+)$/i;
export const MAX_LOG_BYTES = 32 * 1024;
export const MAX_CATALOG_BYTES = 4 * 1024 * 1024;

const UTF8_FATAL = new TextDecoder('utf-8', { fatal: true });
const UTF8_LOSSY = new TextDecoder('utf-8', { fatal: false });
const UTF16LE_DEC = (() => {
  try { return new TextDecoder('utf-16le'); } catch { return null; }
})();
const UTF16BE_DEC = (() => {
  try { return new TextDecoder('utf-16be'); } catch { return null; }
})();
const GB18030_DEC = (() => {
  try { return new TextDecoder('gb18030'); } catch { return null; }
})();

export function normalizeLsHostname(hostname) {
  return String(hostname || '').trim().replace(/^\[|\]$/g, '');
}

export function isLoopbackHostname(hostname) {
  const h = normalizeLsHostname(hostname).toLowerCase();
  return (
    h === 'localhost'
    || h === '127.0.0.1'
    || h === '::1'
    || h === '::ffff:127.0.0.1'
    || h === '::ffff:7f00:1'
    || h === '::ffff:7f00:0001'
  );
}

export function normalizeFileUri(uri) {
  // Normalize Windows file URIs emitted with percent-encoded drive colons
  // (e.g. file:///e%3A/repo on Windows Electron/VS Code, which older Node 18 rejects)
  // or two-slash drive letters (file://C:/repo).
  let normalized = uri.replace(/^file:\/\/([a-zA-Z])(?::|%3[aA])(?=\/|$)/, 'file:///$1:');
  normalized = normalized.replace(/^file:\/\/\/([a-zA-Z])%3[aA](?=\/|$)/, 'file:///$1:');
  return normalized;
}

export function workspacePath(uri) {
  if (!uri || typeof uri !== 'string' || !uri.startsWith('file:')) return '';
  try {
    const p = fileURLToPath(normalizeFileUri(uri));
    if (process.platform === 'win32' && /^[a-z]:\\/.test(p)) {
      return p[0].toUpperCase() + p.slice(1);
    }
    return p;
  } catch {
    return '';
  }
}

export function resolveAppPaths(env = process.env, fallbackHome = homedir()) {
  const explicitGemini = (env.ANTIGRAVITY_GEMINI_DIR || '').trim();
  if (explicitGemini) {
    const appDirName = (env.ANTIGRAVITY_APP_DIR || '').trim() || 'antigravity';
    return {
      geminiDir: explicitGemini,
      appDataDir: join(explicitGemini, appDirName),
      appDirName,
      explicitGeminiDir: true,
    };
  }
  const explicitAppData = (env.ANTIGRAVITY_APP_DATA_DIR || '').trim();
  if (explicitAppData) {
    return {
      geminiDir: dirname(explicitAppData),
      appDataDir: explicitAppData,
      appDirName: basename(explicitAppData) || 'antigravity',
      explicitGeminiDir: false,
    };
  }
  const execDataDir = (env.ANTIGRAVITY_EXECUTABLE_DATA_DIR || '').trim();
  if (execDataDir) {
    for (let d = execDataDir; d !== dirname(d); d = dirname(d)) {
      if (basename(d).toLowerCase() === 'sidecar_data') {
        const appDataDir = dirname(d);
        return {
          geminiDir: dirname(appDataDir),
          appDataDir,
          appDirName: basename(appDataDir) || 'antigravity',
          explicitGeminiDir: false,
        };
      }
    }
  }
  const geminiDir = join(fallbackHome, '.gemini');
  return {
    geminiDir,
    appDataDir: join(geminiDir, 'antigravity'),
    appDirName: 'antigravity',
    explicitGeminiDir: false,
  };
}

export function fileStamp(path) {
  try {
    const s = statSync(path);
    return `${s.mtimeMs}:${s.size}`;
  } catch {
    return '-';
  }
}

export function isWindowsStoreStub(exePath, fsOps = { readlinkSync, statSync }) {
  if (!exePath || typeof exePath !== 'string') return false;
  const norm = exePath.replace(/\//g, '\\').toLowerCase();
  if (!norm.includes('\\microsoft\\windowsapps\\')) return false;
  try {
    // On Windows 10/11, both uninstalled Store stubs and installed Store packages are
    // IO_REPARSE_TAG_APPEXECLINK entries that fail statSync() with EACCES, whereas
    // readlinkSync() returns the actual AppX target path cleanly.
    const target = String(fsOps.readlinkSync(exePath) || '').toLowerCase();
    if (!target) return true;
    return target.includes('appinstallerpythonredirector') || target.includes('microsoft.desktopappinstaller');
  } catch {
    try {
      const st = fsOps.statSync(exePath);
      return !st.isFile() || st.size === 0;
    } catch {
      return true;
    }
  }
}

export function buildChildEnv(cmd, baseEnv = process.env, platform = process.platform) {
  const env = {
    ...baseEnv,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONDONTWRITEBYTECODE: '1',
  };
  if (platform !== 'win32') return env;

  const pathKeys = Object.keys(env).filter((k) => k.toLowerCase() === 'path');
  const canonicalPathKey = pathKeys[0] || 'Path';
  const rawParts = [];
  for (const k of pathKeys) {
    for (const seg of String(env[k] || '').split(';')) {
      const cleaned = seg.trim().replace(/^"+|"+$/g, '');
      if (cleaned) rawParts.push(cleaned);
    }
    if (k !== canonicalPathKey) delete env[k];
  }
  const cmdStr = String(cmd || '');
  const isBarePython = !cmdStr.includes('\\') && !cmdStr.includes('/') && /^python3?(?:\.exe)?$/i.test(cmdStr);

  let filtered = rawParts;
  if (isBarePython) {
    const exeName = cmdStr.toLowerCase().endsWith('.exe') ? cmdStr : `${cmdStr}.exe`;
    filtered = rawParts.filter((dir) => {
      if (!dir.replace(/\//g, '\\').toLowerCase().includes('\\microsoft\\windowsapps')) {
        return true;
      }
      return !isWindowsStoreStub(winPath.join(dir, exeName));
    });
  }

  // Ensure Conda/Anaconda/Miniconda DLL directories (<prefix>\Library\bin) are on Path
  // so older Windows Conda Python 3.9/3.10 builds always locate sqlite3.dll.
  if ((cmdStr.includes('\\') || cmdStr.includes('/')) && /python[^\\/]*\.exe$/i.test(cmdStr)) {
    const pyDir = dirname(cmdStr);
    const extraDirs = [join(pyDir, 'Library', 'bin'), join(pyDir, 'DLLs'), pyDir].filter((d) => {
      try { return statSync(d).isDirectory(); } catch { return false; }
    });
    if (extraDirs.length) {
      const lowerExisting = new Set(filtered.map((p) => p.toLowerCase()));
      const prepend = extraDirs.filter((d) => !lowerExisting.has(d.toLowerCase()));
      filtered = [...prepend, ...filtered];
    }
  }

  env[canonicalPathKey] = filtered.join(';');
  return env;
}

export function execAsync(cmd, args, timeout = 20000) {
  return new Promise((resolve, reject) => {
    execFile(
      cmd,
      args,
      {
        timeout,
        maxBuffer: 16 * 1024 * 1024,
        env: buildChildEnv(cmd),
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

function listMatchingSubdirsDesc(parentDir, prefixRe) {
  try {
    return readdirSync(parentDir, { withFileTypes: true })
      .filter((d) => d.isDirectory() && prefixRe.test(d.name))
      .map((d) => d.name)
      .sort((a, b) => b.localeCompare(a, undefined, { numeric: true, sensitivity: 'base' }))
      .map((name) => join(parentDir, name));
  } catch {
    return [];
  }
}

export function discoverWindowsPythonCandidates(env = process.env, home = homedir()) {
  const seen = new Set();
  const out = [];
  const add = (cmd, pre = [], requireFile = false) => {
    if (!cmd || typeof cmd !== 'string') return;
    const trimmed = cmd.trim().replace(/^"+|"+$/g, '');
    if (!trimmed) return;
    if (isWindowsStoreStub(trimmed)) return;
    if (requireFile) {
      try {
        const st = statSync(trimmed);
        if (!st.isFile() || st.size === 0) return;
      } catch {
        return;
      }
    }
    const key = `${trimmed.toLowerCase()}\0${pre.join('\0')}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push([trimmed, pre]);
  };

  if (env.ANTIGRAVITY_PYTHON) add(env.ANTIGRAVITY_PYTHON, []);

  // Standard PATH launchers & interpreters (Store stubs stripped in buildChildEnv)
  add('py', ['-3']);
  const sysRoot = env.SystemRoot || env.WINDIR || 'C:\\Windows';
  const localAppData = env.LOCALAPPDATA || (home ? join(home, 'AppData', 'Local') : '');
  const appData = env.APPDATA || (home ? join(home, 'AppData', 'Roaming') : '');
  const progFiles = env.ProgramFiles || 'C:\\Program Files';
  const progFilesX86 = env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)';
  const progData = env.ProgramData || 'C:\\ProgramData';
  const sysDrive = (env.SystemDrive || 'C:').replace(/[\\/]+$/, '');

  add(join(sysRoot, 'py.exe'), ['-3'], true);
  if (localAppData) add(join(localAppData, 'Programs', 'Python', 'Launcher', 'py.exe'), ['-3'], true);

  add('python', []);
  add('python3', []);

  if (env.VIRTUAL_ENV) add(join(env.VIRTUAL_ENV, 'Scripts', 'python.exe'), [], true);
  if (env.CONDA_PREFIX) add(join(env.CONDA_PREFIX, 'python.exe'), [], true);

  // Official Python.org per-user & system-wide installs (Python314 .. Python39, x64/ARM64/x86)
  const pyFolderRoots = [
    localAppData ? join(localAppData, 'Programs', 'Python') : '',
    progFiles,
    progFilesX86,
    `${sysDrive}\\`,
  ].filter(Boolean);
  for (const root of pyFolderRoots) {
    for (const dir of listMatchingSubdirsDesc(root, /^Python3\d+/i)) {
      add(join(dir, 'python.exe'), [], true);
    }
  }

  // Conda / Miniconda / Miniforge / Mambaforge common roots
  const condaNames = ['miniconda3', 'anaconda3', 'miniforge3', 'mambaforge'];
  for (const base of [home, localAppData, progData, `${sysDrive}\\tools`].filter(Boolean)) {
    for (const name of condaNames) {
      add(join(base, name, 'python.exe'), [], true);
    }
  }

  // uv, Scoop, and pyenv-win managed interpreters
  for (const uvRoot of [
    appData ? join(appData, 'uv', 'python') : '',
    localAppData ? join(localAppData, 'uv', 'python') : '',
  ].filter(Boolean)) {
    for (const dir of listMatchingSubdirsDesc(uvRoot, /^cpython-3\./i)) {
      add(join(dir, 'python.exe'), [], true);
    }
  }
  if (home) {
    add(join(home, 'scoop', 'apps', 'python', 'current', 'python.exe'), [], true);
    for (const dir of listMatchingSubdirsDesc(join(home, '.pyenv', 'pyenv-win', 'versions'), /^3\./)) {
      add(join(dir, 'python.exe'), [], true);
    }
  }

  // Real Microsoft Store Python installation (APPEXECLINK entries fail statSync with EACCES
  // even when installed, so verify existence via lstatSync + non-redirector readlinkSync target)
  if (localAppData) {
    const winAppsRoot = join(localAppData, 'Microsoft', 'WindowsApps');
    const addStoreAlias = (aliasPath) => {
      try {
        lstatSync(aliasPath);
      } catch {
        return;
      }
      if (!isWindowsStoreStub(aliasPath)) {
        add(aliasPath, [], false);
      }
    };
    for (const dir of listMatchingSubdirsDesc(winAppsRoot, /^PythonSoftwareFoundation\.Python\.3\./i)) {
      addStoreAlias(join(dir, 'python.exe'));
      addStoreAlias(join(dir, 'python3.exe'));
    }
    addStoreAlias(join(winAppsRoot, 'python3.exe'));
    addStoreAlias(join(winAppsRoot, 'python.exe'));
  }

  return out;
}

function expandWindowsEnvVars(value, env = process.env) {
  if (!value || !value.includes('%')) return value;
  const envMap = new Map(Object.entries(env || {}).map(([k, v]) => [k.toLowerCase(), String(v ?? '')]));
  return value.replace(/%([^%]+)%/g, (whole, name) => {
    const key = String(name).toLowerCase();
    return envMap.has(key) ? envMap.get(key) : whole;
  });
}

export function parseWindowsRegistryPythonOutput(regStdout, env = process.env) {
  if (!regStdout || typeof regStdout !== 'string') return [];
  const entries = [];
  let currentKey = '';
  let currentVer = '';
  let isPyLauncher = false;
  let isInstallPath = false;

  for (const rawLine of regStdout.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    if (/^HKEY_/i.test(line)) {
      currentKey = line;
      const verMatch = /\\PythonCore\\(\d+\.\d+)(?:-[^\\]+)?\\InstallPath$/i.exec(currentKey);
      currentVer = verMatch ? verMatch[1] : '';
      isInstallPath = Boolean(verMatch);
      isPyLauncher = /\\PyLauncher$/i.test(currentKey);
      continue;
    }
    if (!isInstallPath && !isPyLauncher) continue;

    // Locale-independent REG_SZ / REG_EXPAND_SZ parsing: works on English "(Default)",
    // Chinese "(默认)", and any localized Windows reg.exe output.
    const valMatch = /^(.+?)\s+(REG_(?:EXPAND_)?SZ)\s+(.+)$/i.exec(line);
    if (!valMatch) continue;
    const valueName = valMatch[1].trim().toLowerCase();
    const regType = valMatch[2].toUpperCase();
    const unexpanded = valMatch[3].trim().replace(/^"+|"+$/g, '');
    const rawVal = regType === 'REG_EXPAND_SZ' ? expandWindowsEnvVars(unexpanded, env) : unexpanded;
    if (!rawVal) continue;

    if (isPyLauncher) {
      if (rawVal.toLowerCase().endsWith('py.exe')) {
        const normalized = winPath.normalize(rawVal);
        if (winPath.isAbsolute(normalized)) {
          entries.push({ ver: '999.0', priority: 2, cmd: normalized, pre: ['-3'] });
        }
      } else if (valueName === 'installdir') {
        const normalized = winPath.normalize(winPath.join(rawVal, 'py.exe'));
        if (winPath.isAbsolute(normalized)) {
          entries.push({ ver: '999.0', priority: 1, cmd: normalized, pre: ['-3'] });
        }
      }
      continue;
    }

    const major = Number(currentVer.split('.')[0]);
    if (!Number.isFinite(major) || major < 3) continue;
    if (valueName === 'windowedexecutablepath') continue;
    if (valueName === 'executablepath') {
      const normalized = winPath.normalize(rawVal);
      if (winPath.isAbsolute(normalized)) {
        entries.push({ ver: currentVer, priority: 2, cmd: normalized, pre: [] });
      }
    } else if (valueName.startsWith('(')) {
      const exe = rawVal.toLowerCase().endsWith('.exe')
        ? winPath.normalize(rawVal)
        : winPath.normalize(winPath.join(rawVal, 'python.exe'));
      if (winPath.isAbsolute(exe)) {
        entries.push({ ver: currentVer, priority: 1, cmd: exe, pre: [] });
      }
    }
  }

  entries.sort((a, b) => {
    const cmp = b.ver.localeCompare(a.ver, undefined, { numeric: true });
    return cmp !== 0 ? cmp : b.priority - a.priority;
  });

  const seen = new Set();
  const out = [];
  for (const e of entries) {
    const key = `${e.cmd.toLowerCase()}\0${e.pre.join('\0')}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push([e.cmd, e.pre]);
  }
  return out;
}

export async function queryWindowsRegistryPython(runner = execAsync, env = process.env) {
  const sysRoot = env.SystemRoot || env.WINDIR || 'C:\\Windows';
  const regExe = existsSync(join(sysRoot, 'System32', 'reg.exe'))
    ? join(sysRoot, 'System32', 'reg.exe')
    : 'reg.exe';
  const roots = [
    'HKCU\\Software\\Python',
    'HKLM\\Software\\Python',
    'HKLM\\Software\\WOW6432Node\\Python',
  ];
  const chunks = [];
  for (const key of roots) {
    try {
      chunks.push(await runner(regExe, ['query', key, '/s'], 3000));
    } catch {
      // Registry branch absent on this machine; continue
    }
  }
  return parseWindowsRegistryPythonOutput(chunks.join('\n'), env);
}

export async function probePython(candidates, runner = execAsync, extraDiscovery = null, deadlineMs = 15000) {
  const probeCode = [
    'import os, sys',
    'if sys.platform == "win32" and hasattr(os, "add_dll_directory"):',
    '  for p in {sys.prefix, getattr(sys, "base_prefix", sys.prefix)}:',
    '    b = os.path.join(p, "Library", "bin")',
    '    if os.path.isdir(b):',
    '      try: os.add_dll_directory(b)',
    '      except OSError: pass',
    'import sqlite3',
    'if sys.version_info < (3, 9): sys.exit(1)',
    'sys.stdout.write(sys.executable or "")',
  ].join('\n');
  const failures = [];
  const tried = new Set();
  const deadline = Date.now() + Math.max(1000, deadlineMs);

  const tryList = async (list) => {
    for (const [cmd, pre] of list) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) {
        failures.push('discovery deadline exceeded');
        break;
      }
      const key = `${cmd}\0${pre.join('\0')}`;
      if (tried.has(key)) continue;
      tried.add(key);
      try {
        const out = await runner(cmd, [...pre, '-c', probeCode], Math.min(5000, remaining));
        const resolved = typeof out === 'string' ? out.trim().split(/\r?\n/)[0].trim() : '';
        if (resolved && (isAbsolute(resolved) || /^[a-zA-Z]:[\\/]/.test(resolved))) {
          return { cmd: resolved, pre: [] };
        }
        return { cmd, pre };
      } catch (err) {
        const label = [cmd, ...pre].join(' ');
        failures.push(`${label}: ${err.message}`);
      }
    }
    return null;
  };

  const primary = await tryList(candidates);
  if (primary) return primary;

  if (typeof extraDiscovery === 'function' && Date.now() < deadline) {
    try {
      const extra = await extraDiscovery();
      if (Array.isArray(extra) && extra.length) {
        const found = await tryList(extra);
        if (found) return found;
      }
    } catch {
      // Ignore secondary discovery errors and surface primary failure details
    }
  }

  throw new Error(
    `Python 3.9+ with sqlite3 not found (${failures.slice(0, 6).join('; ') || 'no candidates'}). `
      + 'Install Python 3.9+ (or set ANTIGRAVITY_PYTHON) and restart Antigravity.',
  );
}

export function isPathStrictlyInside(realRoot, targetPath) {
  const rel = relative(realRoot, targetPath);
  if (!rel || rel === '.' || rel === '..' || isAbsolute(rel)) return false;
  return !rel.startsWith(`..${sep}`) && !rel.startsWith('../') && !rel.startsWith('..\\');
}

function verifySegmentsNoSymlink(rootDir, segments, realRoot) {
  let cur = rootDir;
  let lastStat = null;
  for (let i = 0; i < segments.length; i++) {
    cur = join(cur, segments[i]);
    const lst = lstatSync(cur, { bigint: true });
    if (lst.isSymbolicLink()) return null;
    const isLast = i === segments.length - 1;
    if (isLast ? !lst.isFile() : !lst.isDirectory()) return null;
    lastStat = lst;
  }
  const realTarget = realpathSync(cur);
  if (!isPathStrictlyInside(realRoot, realTarget)) return null;
  return { realTarget, leafStat: lastStat };
}

function looksLikeUtf16LeWithoutBom(buf) {
  if (buf.length < 4) return false;
  const sampleLen = Math.min(buf.length, 512);
  let evenZero = 0;
  let oddZero = 0;
  const pairs = Math.floor(sampleLen / 2);
  for (let i = 0; i < pairs * 2; i += 2) {
    if (buf[i] === 0) evenZero++;
    if (buf[i + 1] === 0) oddZero++;
  }
  return oddZero > pairs * 0.3 && evenZero < pairs * 0.05;
}

function trimTruncatedFirstLine(text, truncated) {
  if (!truncated || !text) return text;
  const nl = text.indexOf('\n');
  if (nl !== -1 && nl < 256) return text.slice(nl + 1);
  return text.replace(/^\uFFFD+/, '');
}

export function decodeLogBuffer(buf, truncated = false, headerBytes = null) {
  if (!buf || buf.length === 0) return '';

  const hdr = headerBytes && headerBytes.length >= 2 ? headerBytes : buf;
  const hasUtf16LeBom = hdr.length >= 2 && hdr[0] === 0xff && hdr[1] === 0xfe;
  const hasUtf16BeBom = hdr.length >= 2 && hdr[0] === 0xfe && hdr[1] === 0xff;

  // UTF-16LE / UTF-16BE BOM (e.g. Windows PowerShell 5.1 Out-File / > redirection),
  // including >32 KiB truncated tails where headerBytes carries byte 0..1 from the file head.
  if (hasUtf16LeBom && UTF16LE_DEC) {
    const start = (!truncated && buf.length >= 2 && buf[0] === 0xff && buf[1] === 0xfe) ? 2 : 0;
    const body = buf.subarray(start, start + ((buf.length - start) & ~1));
    return trimTruncatedFirstLine(UTF16LE_DEC.decode(body), truncated);
  }
  if (hasUtf16BeBom && UTF16BE_DEC) {
    const start = (!truncated && buf.length >= 2 && buf[0] === 0xfe && buf[1] === 0xff) ? 2 : 0;
    const body = buf.subarray(start, start + ((buf.length - start) & ~1));
    return trimTruncatedFirstLine(UTF16BE_DEC.decode(body), truncated);
  }
  if (UTF16LE_DEC && looksLikeUtf16LeWithoutBom(buf)) {
    const start = buf[0] === 0 && buf[1] !== 0 ? 1 : 0;
    const usable = buf.subarray(start, start + ((buf.length - start) & ~1));
    return trimTruncatedFirstLine(UTF16LE_DEC.decode(usable), truncated);
  }

  let start = 0;
  if (truncated) {
    // Skip up to 3 orphaned leading UTF-8 continuation bytes (10xxxxxx)
    while (start < 3 && start < buf.length && (buf[start] & 0xc0) === 0x80) {
      start++;
    }
  }
  let end = buf.length;
  // Trim incomplete multi-byte UTF-8 sequence at live EOF (up to 3 bytes)
  for (let back = 1; back <= 3 && end - back >= start; back++) {
    const b = buf[end - back];
    if ((b & 0x80) === 0) break; // ASCII complete
    if ((b & 0xc0) === 0x80) continue; // continuation byte
    const expectedLen = (b & 0xe0) === 0xc0 ? 2 : (b & 0xf0) === 0xe0 ? 3 : (b & 0xf8) === 0xf0 ? 4 : 1;
    if (back < expectedLen) {
      end -= back;
    }
    break;
  }

  const utf8Slice = buf.subarray(start, end);
  try {
    const text = UTF8_FATAL.decode(utf8Slice);
    return trimTruncatedFirstLine(text, truncated);
  } catch {
    // Fallback for native Windows console/CMD output in GBK/GB18030 (e.g. ipconfig, ping, tasklist)
    if (GB18030_DEC) {
      return trimTruncatedFirstLine(GB18030_DEC.decode(buf), truncated);
    }
    return trimTruncatedFirstLine(UTF8_LOSSY.decode(utf8Slice), truncated);
  }
}

function candidateLogConversations(conversationId, idxStr, taskId) {
  const ids = [conversationId];
  if (typeof taskId === 'string' && taskId) {
    const m = FORKED_TASK_ID_RE.exec(taskId.trim());
    if (m && m[2] === idxStr && m[1].toLowerCase() !== conversationId.toLowerCase()) {
      ids.push(m[1]);
    }
  }
  return ids;
}

export function readTaskLogSafe(geminiDir, conversationId, stepIdx, options = {}) {
  const idxNum = typeof stepIdx === 'number'
    ? stepIdx
    : (typeof stepIdx === 'string' && /^\d+$/.test(stepIdx) ? Number(stepIdx) : NaN);
  if (
    typeof conversationId !== 'string'
    || !UUID.test(conversationId)
    || !Number.isInteger(idxNum)
    || idxNum < 0
    || idxNum > 1_000_000
  ) {
    return { error: 'invalid-params' };
  }
  const idxStr = String(idxNum);
  const opts = typeof options === 'string' ? { taskId: options } : (options || {});
  const appDirName = opts.appDirName || 'antigravity';
  const empty = { exists: false, content: '', size: 0, mtimeMs: 0, truncated: false };

  let realRoot;
  try {
    realRoot = realpathSync(geminiDir);
  } catch {
    return empty;
  }

  const candidateConvs = candidateLogConversations(conversationId, idxStr, opts.taskId);
  let chosenSegments = null;
  let preCheck = null;
  for (const cid of candidateConvs) {
    const segments = [appDirName, 'brain', cid, '.system_generated', 'tasks', `task-${idxStr}.log`];
    try {
      const checked = verifySegmentsNoSymlink(geminiDir, segments, realRoot);
      if (checked) {
        chosenSegments = segments;
        preCheck = checked;
        break;
      }
    } catch {
      // Try next candidate (e.g. parent conversation of a forked session)
    }
  }
  if (!preCheck || !chosenSegments) return empty;

  const flags =
    constants.O_RDONLY
    | (typeof constants.O_NOFOLLOW === 'number' ? constants.O_NOFOLLOW : 0)
    | (typeof constants.O_NONBLOCK === 'number' ? constants.O_NONBLOCK : 0);
  let fd;
  try {
    fd = openSync(preCheck.realTarget, flags);
  } catch (err) {
    if (
      err
      && (
        err.code === 'ENOENT'
        || err.code === 'ELOOP'
        || err.code === 'ENOTDIR'
        || err.code === 'ENXIO'
        || err.code === 'EISDIR'
        || (process.platform === 'win32' && (err.code === 'EACCES' || err.code === 'EPERM' || err.code === 'EBUSY'))
      )
    ) {
      return empty;
    }
    throw err;
  }

  try {
    const stat = fstatSync(fd, { bigint: true });
    if (!stat.isFile()) return empty;
    // Post-open re-verification: confirm ancestors still contain no symlinks/junctions and the
    // opened file descriptor matches the exact resolved leaf file inside realRoot (64-bit safe).
    const postRoot = realpathSync(geminiDir);
    const postCheck = verifySegmentsNoSymlink(geminiDir, chosenSegments, postRoot);
    if (!postCheck || postCheck.realTarget !== preCheck.realTarget) {
      return empty;
    }
    if (
      (postCheck.leafStat.dev !== 0n && stat.dev !== 0n && postCheck.leafStat.dev !== stat.dev)
      || (postCheck.leafStat.ino !== 0n && stat.ino !== 0n && postCheck.leafStat.ino !== stat.ino)
    ) {
      return empty;
    }
    const fileSize = Number(stat.size);
    const truncated = fileSize > MAX_LOG_BYTES;
    let headerBytes = null;
    if (fileSize >= 2) {
      headerBytes = Buffer.allocUnsafe(2);
      const hn = readSync(fd, headerBytes, 0, 2, 0);
      if (hn < 2) headerBytes = null;
    }
    const isUtf16Bom = Boolean(
      headerBytes
      && ((headerBytes[0] === 0xff && headerBytes[1] === 0xfe) || (headerBytes[0] === 0xfe && headerBytes[1] === 0xff)),
    );
    let toRead = Math.min(fileSize, MAX_LOG_BYTES);
    let offset = fileSize - toRead;
    if (truncated && isUtf16Bom && (offset % 2 !== 0)) {
      offset += 1;
      toRead = Math.max(0, fileSize - offset);
    }
    let content = '';
    if (toRead > 0) {
      const buf = Buffer.allocUnsafe(toRead);
      let readTotal = 0;
      while (readTotal < toRead) {
        const n = readSync(fd, buf, readTotal, toRead - readTotal, offset + readTotal);
        if (n <= 0) break;
        readTotal += n;
      }
      content = decodeLogBuffer(buf.subarray(0, readTotal), truncated, headerBytes);
    }
    return {
      exists: true,
      content,
      size: fileSize,
      mtimeMs: Number(stat.mtimeMs),
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

export function writeJsonAtomic(filePath, data, fsOps = { renameSync, writeFileSync, unlinkSync, existsSync, mkdirSync }) {
  const dir = dirname(filePath);
  fsOps.mkdirSync(dir, { recursive: true });
  const serialized = JSON.stringify(data);
  const tmpPath = `${filePath}.tmp.${process.pid}.${Date.now()}.${Math.random().toString(36).slice(2, 8)}`;
  try {
    fsOps.writeFileSync(tmpPath, serialized, 'utf8');
    let lastErr = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        fsOps.renameSync(tmpPath, filePath);
        return;
      } catch (renameErr) {
        lastErr = renameErr;
        if (
          renameErr
          && (renameErr.code === 'EPERM' || renameErr.code === 'EACCES' || renameErr.code === 'EBUSY')
          && attempt < 2
        ) {
          continue;
        }
        break;
      }
    }
    throw lastErr;
  } catch (err) {
    try {
      if (fsOps.existsSync(tmpPath)) fsOps.unlinkSync(tmpPath);
    } catch { /* ignore cleanup error */ }
    throw err;
  }
}

export function systemFileStampPaths(geminiDir, workspace, appDirName = 'antigravity') {
  const paths = [
    geminiDir,
    join(geminiDir, 'GEMINI.md'),
    join(geminiDir, appDirName),
  ];
  if (workspace) {
    for (const root of ['.agents', '_agents']) {
      paths.push(join(workspace, root), join(workspace, root, 'hooks.json'));
    }
  }
  return paths;
}

export function collectDependencyStamps(reportData, conversationsDir, tasksDir, geminiDir = '', workspace = '', appDirName = 'antigravity') {
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
    for (const p of systemFileStampPaths(geminiDir, workspace, appDirName)) addPath(p, false);
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
  const missingSubIds = Array.isArray(reportData?.cost_stats?.missing_subagent_ids)
    ? reportData.cost_stats.missing_subagent_ids
    : [];
  for (const sid of missingSubIds) {
    if (typeof sid === 'string' && UUID.test(sid)) {
      const subDb = join(conversationsDir, `${sid}.db`);
      addPath(subDb, false);
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
