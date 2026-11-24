import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  buildChildEnv,
  collectDependencyStamps,
  decodeLogBuffer,
  dependencyStampsMatch,
  discoverWindowsPythonCandidates,
  isLoopbackHostname,
  isWindowsStoreStub,
  normalizeCatalogResponse,
  normalizeFileUri,
  normalizeLsHostname,
  parseWindowsRegistryPythonOutput,
  probePython,
  readTaskLogSafe,
  resolveAppPaths,
  snapshotDependencyStamps,
  stampsUnchanged,
  workspacePath,
  writeJsonAtomic,
} from '../sidecars/context/sidecar_utils.mjs';

test('workspacePath handles local, localhost, Windows, and encoded paths', () => {
  assert.equal(workspacePath(''), '');
  assert.equal(workspacePath('https://example.com/ws'), '');
  assert.equal(normalizeFileUri('file:///e%3A/project1/repo'), 'file:///e:/project1/repo');
  assert.equal(normalizeFileUri('file://C:/project1/repo'), 'file:///C:/project1/repo');
  if (process.platform === 'win32') {
    assert.equal(workspacePath('file:///C:/my%20repo'), 'C:\\my repo');
    assert.equal(workspacePath('file:///e%3A/project1/antigravity-status-bar'), 'E:\\project1\\antigravity-status-bar');
    assert.equal(workspacePath('file://c:/repo'), 'C:\\repo');
    assert.equal(workspacePath('file://localhost/C:/repo'), 'C:\\repo');
    assert.equal(workspacePath('file://server/share/dir'), '\\\\server\\share\\dir');
  } else {
    assert.equal(workspacePath('file:///tmp/my%20repo'), '/tmp/my repo');
    assert.equal(workspacePath('file://localhost/tmp/repo'), '/tmp/repo');
  }
});

test('probePython falls back past incompatible/failing interpreters', async () => {
  const calls = [];
  const fakeRunner = async (cmd, args) => {
    calls.push([cmd, ...args]);
    if (cmd === 'python') throw new Error('Python 3.8 unsupported');
    return '';
  };
  const chosen = await probePython([['python', []], ['py', ['-3']]], fakeRunner);
  assert.deepEqual(chosen, { cmd: 'py', pre: ['-3'] });
  assert.equal(calls.length, 2);

  await assert.rejects(
    probePython([['python', []]], async () => { throw new Error('missing'); }),
    /Python 3\.9\+ with sqlite3 not found/,
  );
});

test('readTaskLogSafe enforces root containment for both leaf and ancestor symlinks', () => {
  const tmp = mkdtempSync(join(tmpdir(), 'sb-log-'));
  try {
    const cid = '11111111-1111-1111-1111-111111111111';
    const cidAncestor = '33333333-3333-3333-3333-333333333333';
    const tasksDir = join(tmp, 'antigravity', 'brain', cid, '.system_generated', 'tasks');
    mkdirSync(tasksDir, { recursive: true });

    const outsideDir = join(tmp, 'outside', '.system_generated', 'tasks');
    mkdirSync(outsideDir, { recursive: true });
    const secret = join(outsideDir, 'task-0.log');
    writeFileSync(secret, 'TOP_SECRET', 'utf8');

    const log0 = join(tasksDir, 'task-0.log');
    writeFileSync(log0, 'hello\nworld\n', 'utf8');

    const ok = readTaskLogSafe(tmp, cid, 0);
    assert.equal(ok.exists, true);
    assert.equal(ok.content, 'hello\nworld\n');

    // Missing log or invalid step index
    assert.equal(readTaskLogSafe(tmp, cid, 99).exists, false);
    assert.deepEqual(readTaskLogSafe(tmp, 'not-a-uuid', 0), { error: 'invalid-params' });
    assert.deepEqual(readTaskLogSafe(tmp, cid, '../0'), { error: 'invalid-params' });

    if (process.platform === 'win32') {
      // Windows NTFS junction redirecting brain/<cidAncestor> outside must be blocked without admin privileges
      const linkedCidDir = join(tmp, 'antigravity', 'brain', cidAncestor);
      symlinkSync(join(tmp, 'outside'), linkedCidDir, 'junction');
      const blockedAncestor = readTaskLogSafe(tmp, cidAncestor, 0);
      assert.equal(blockedAncestor.exists, false);

      // Also test leaf file symlink when Developer Mode / SeCreateSymbolicLinkPrivilege is enabled
      const linkLog = join(tasksDir, 'task-1.log');
      let leafCreated = false;
      try {
        symlinkSync(secret, linkLog, 'file');
        leafCreated = true;
      } catch (err) {
        if (err.code !== 'EPERM') throw err;
      }
      if (leafCreated) {
        assert.equal(readTaskLogSafe(tmp, cid, 1).exists, false);
      }
    } else {
      // Leaf symlink pointing outside tasksDir must be blocked
      const linkLog = join(tasksDir, 'task-1.log');
      symlinkSync(secret, linkLog);
      const blockedLeaf = readTaskLogSafe(tmp, cid, 1);
      assert.equal(blockedLeaf.exists, false);

      // Ancestor symlink redirecting brain/<cidAncestor> outside must be blocked
      const linkedCidDir = join(tmp, 'antigravity', 'brain', cidAncestor);
      symlinkSync(join(tmp, 'outside'), linkedCidDir);
      const blockedAncestor = readTaskLogSafe(tmp, cidAncestor, 0);
      assert.equal(blockedAncestor.exists, false);
    }
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('normalizeCatalogResponse clamps negative maxTokens and writes atomically', () => {
  const tmp = mkdtempSync(join(tmpdir(), 'sb-cat-'));
  try {
    const normalized = normalizeCatalogResponse({
      response: {
        models: {
          'gemini-4-hydrogen-high': { maxTokens: 1048576, displayName: 'Gemini 4 Hydrogen (High)' },
          'display-only': { maxTokens: -50, displayName: 'Label Only' },
          empty: { maxTokens: 0, displayName: '' },
        },
      },
    });
    assert.deepEqual(normalized, {
      'gemini-4-hydrogen-high': { maxTokens: 1048576, displayName: 'Gemini 4 Hydrogen (High)' },
      'display-only': { maxTokens: 0, displayName: 'Label Only' },
    });

    const target = join(tmp, 'nested', 'model-catalog.json');
    writeJsonAtomic(target, normalized);
    assert.deepEqual(JSON.parse(readFileSync(target, 'utf8')), normalized);
    assert.equal(isLoopbackHostname('127.0.0.1'), true);
    assert.equal(isLoopbackHostname('localhost'), true);
    assert.equal(isLoopbackHostname('example.com'), false);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('dependencyStampsMatch and stampsUnchanged detect mid-run child/log updates', () => {
  const tmp = mkdtempSync(join(tmpdir(), 'sb-deps-'));
  try {
    const geminiDir = join(tmp, '.gemini');
    const convDir = join(geminiDir, 'antigravity', 'conversations');
    const tasksDir = join(tmp, 'tasks');
    const customLog = join(tmp, 'custom-task.log');
    mkdirSync(convDir, { recursive: true });
    mkdirSync(tasksDir, { recursive: true });
    const subId = '22222222-2222-2222-2222-222222222222';
    const subDb = join(convDir, `${subId}.db`);
    writeFileSync(subDb, 'v1', 'utf8');
    writeFileSync(customLog, 'log1', 'utf8');

    const stamps = collectDependencyStamps(
      {
        background_tasks: [
          { tool: 'invoke_subagent', task_id: subId },
          { tool: 'run_command', step_idx: 3, log_path: customLog },
        ],
      },
      convDir,
      tasksDir,
      geminiDir,
      '',
    );
    const before = snapshotDependencyStamps(stamps);
    assert.equal(dependencyStampsMatch(stamps), true);
    assert.equal(stampsUnchanged(before, stamps), true);

    // Newly discovered dependency modified around/during run must invalidate caching
    const newSubId = '44444444-4444-4444-4444-444444444444';
    writeFileSync(join(convDir, `${newSubId}.db`), 'fresh', 'utf8');
    const afterWithNew = collectDependencyStamps(
      { background_tasks: [{ tool: 'invoke_subagent', task_id: newSubId, status: 'done' }] },
      convDir,
      tasksDir,
      geminiDir,
      '',
    );
    assert.equal(stampsUnchanged(before, afterWithNew, Date.now()), false);

    // Disappearing expected dependency (subagent DB or upgraded log_path has_log:true) must invalidate caching
    const vanishedStamps = collectDependencyStamps(
      { background_tasks: [{ tool: 'invoke_subagent', task_id: '55555555-5555-5555-5555-555555555555', status: 'done' }] },
      convDir,
      tasksDir,
      geminiDir,
      '',
    );
    assert.equal(stampsUnchanged(before, vanishedStamps, Date.now()), false);

    const upgradedMissingLog = join(tasksDir, 'task-7.log');
    const logUpgradedStamps = collectDependencyStamps(
      {
        background_tasks: [
          { tool: 'run_command', step_idx: 7, log_path: '', has_log: false },
          { tool: 'run_command', step_idx: 7, log_path: upgradedMissingLog, has_log: true },
        ],
      },
      convDir,
      tasksDir,
      geminiDir,
      '',
    );
    assert.equal(stampsUnchanged(before, logUpgradedStamps, Date.now()), false);

    writeFileSync(subDb, 'v2-longer', 'utf8');
    assert.equal(dependencyStampsMatch(stamps), false);
    assert.equal(stampsUnchanged(before, collectDependencyStamps({ background_tasks: [{ tool: 'invoke_subagent', task_id: subId }] }, convDir, tasksDir, geminiDir, '')), false);

    // Nested descendant subagent IDs inside cost_stats also trigger cache invalidation
    const grandId = '66666666-6666-6666-6666-666666666666';
    const grandDb = join(convDir, `${grandId}.db`);
    writeFileSync(grandDb, 'g1', 'utf8');
    const nestedStamps = collectDependencyStamps(
      { cost_stats: { subagent_conversation_ids: [grandId] } },
      convDir,
      tasksDir,
      geminiDir,
      '',
    );
    assert.equal(dependencyStampsMatch(nestedStamps), true);
    writeFileSync(grandDb, 'g2-updated', 'utf8');
    assert.equal(dependencyStampsMatch(nestedStamps), false);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('Windows Python discovery, PEP 514 registry parsing, Conda DLL Path, and Store stub filtering', async () => {
  const regOut = [
    'HKEY_CURRENT_USER\\Software\\Python\\PythonCore\\3.11\\InstallPath',
    '    (默认)    REG_SZ    D:\\python\\',
    '    ExecutablePath    REG_SZ    D:\\python\\python.exe',
    '    WindowedExecutablePath    REG_SZ    D:\\python\\pythonw.exe',
    '',
    'HKEY_LOCAL_MACHINE\\Software\\Python\\PythonCore\\3.12\\InstallPath',
    '    (Default)    REG_SZ    C:\\Program Files\\Python312',
    '',
    'HKEY_CURRENT_USER\\Software\\Python\\PythonCore\\3.13\\InstallPath',
    '    ExecutablePath    REG_EXPAND_SZ    %LOCALAPPDATA%\\Programs\\Python\\Python313\\python.exe',
    '',
    'HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Python\\PyLauncher',
    '    InstallDir    REG_SZ    C:\\Windows\\',
    '    (Default)    REG_SZ    C:\\Windows\\py.exe',
    '',
    'HKEY_LOCAL_MACHINE\\Software\\WOW6432Node\\Python\\PythonCore\\2.7\\InstallPath',
    '    (Default)    REG_SZ    E:\\Python27\\ArcGIS10.8\\',
  ].join('\r\n');

  const parsed = parseWindowsRegistryPythonOutput(regOut, { LOCALAPPDATA: 'C:\\Users\\alice\\AppData\\Local' });
  assert.deepEqual(parsed, [
    ['C:\\Windows\\py.exe', ['-3']],
    ['C:\\Users\\alice\\AppData\\Local\\Programs\\Python\\Python313\\python.exe', []],
    ['C:\\Program Files\\Python312\\python.exe', []],
    ['D:\\python\\python.exe', []],
  ]);

  // APPEXECLINK detection distinguishes uninstalled Store redirector from real Store Python package
  const aliasPath = 'C:\\Users\\alice\\AppData\\Local\\Microsoft\\WindowsApps\\python3.exe';
  assert.equal(
    isWindowsStoreStub(aliasPath, {
      readlinkSync: () => 'C:\\Program Files\\WindowsApps\\Microsoft.DesktopAppInstaller_1.29_x64__8wekyb3d8bbwe\\AppInstallerPythonRedirector.exe',
      statSync: () => { throw Object.assign(new Error('EACCES'), { code: 'EACCES' }); },
    }),
    true,
  );
  assert.equal(
    isWindowsStoreStub(aliasPath, {
      readlinkSync: () => 'C:\\Program Files\\WindowsApps\\PythonSoftwareFoundation.Python.3.12_3.12.2032.0_x64__qbz5n2kfra8p0\\python3.12.exe',
      statSync: () => { throw Object.assign(new Error('EACCES'), { code: 'EACCES' }); },
    }),
    false,
  );

  // Secondary registry discovery kicks in when primary candidates fail
  const chosen = await probePython(
    [['missing-py', []]],
    async (cmd) => {
      if (cmd === 'missing-py') throw new Error('ENOENT');
      return '';
    },
    async () => [['D:\\python\\python.exe', []]],
  );
  assert.deepEqual(chosen, { cmd: 'D:\\python\\python.exe', pre: [] });

  // Conda Library\bin injection and case-preserving Path key on Windows
  const tmp = mkdtempSync(join(tmpdir(), 'sb-conda-'));
  try {
    const condaRoot = join(tmp, 'Anaconda3');
    const libBin = join(condaRoot, 'Library', 'bin');
    const winApps = join(tmp, 'Microsoft', 'WindowsApps');
    mkdirSync(libBin, { recursive: true });
    mkdirSync(winApps, { recursive: true });
    const condaPy = join(condaRoot, 'python.exe');
    writeFileSync(condaPy, 'MZ', 'utf8');

    const childEnv = buildChildEnv(condaPy, { Path: `C:\\Windows\\system32;${winApps}` }, 'win32');
    assert.ok(childEnv.Path.startsWith(libBin));
    assert.equal(childEnv.PYTHONDONTWRITEBYTECODE, '1');

    // Bare 'python' strips missing/zero-byte WindowsApps stub directory from Path
    const bareEnv = buildChildEnv('python', { Path: `C:\\Windows\\system32;${winApps}` }, 'win32');
    assert.ok(!bareEnv.Path.includes('WindowsApps'));

    const cands = discoverWindowsPythonCandidates({ CONDA_PREFIX: condaRoot, SystemRoot: 'C:\\Windows' }, tmp);
    assert.ok(cands.some(([c]) => c === condaPy));
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

test('decodeLogBuffer, forked conversation logs, resolveAppPaths, IPv6 host normalization, and writeJsonAtomic EPERM fallback', () => {
  // UTF-16LE with BOM (Windows PowerShell 5.1 Out-File default)
  const utf16Header = Buffer.from([0xff, 0xfe]);
  const utf16Body = Buffer.from('PowerShell 中文输出\r\n第二行\r\n', 'utf16le');
  assert.equal(decodeLogBuffer(Buffer.concat([utf16Header, utf16Body]), false), 'PowerShell 中文输出\r\n第二行\r\n');

  // Truncated multi-byte UTF-8 boundary + incomplete trailing 3-byte Chinese char at live EOF
  const fullUtf8 = Buffer.from('第一行被截断\n正常中文日志行\n', 'utf8');
  const midSliced = Buffer.concat([fullUtf8.subarray(1), Buffer.from([0xe4, 0xb8])]); // orphaned leading byte + incomplete trailing char
  assert.equal(decodeLogBuffer(midSliced, true), '正常中文日志行\n');

  // GB18030 / GBK bytes from native Windows console tools (e.g. "正在 Ping" = D5 FD D4 DA 20 50 69 6E 67)
  const gbkPing = Buffer.from([0xd5, 0xfd, 0xd4, 0xda, 0x20, 0x50, 0x69, 0x6e, 0x67]);
  assert.equal(decodeLogBuffer(gbkPing, false), '正在 Ping');

  // Forked conversation task log resolution + strict step mismatch rejection
  const tmp = mkdtempSync(join(tmpdir(), 'sb-fork-'));
  try {
    const parentCid = '59fc7f82-638b-4aa3-ac89-c8dea00cc170';
    const childCid = 'b973153d-4fdb-44f3-b0dd-ed6b5f0d5b87';
    const parentTasks = join(tmp, 'antigravity', 'brain', parentCid, '.system_generated', 'tasks');
    mkdirSync(parentTasks, { recursive: true });
    writeFileSync(join(parentTasks, 'task-248.log'), 'forked task output\r\n', 'utf8');

    const okForked = readTaskLogSafe(tmp, childCid, 248, { taskId: `${parentCid}/task-248` });
    assert.equal(okForked.exists, true);
    assert.equal(okForked.content, 'forked task output\r\n');

    // Mismatched step number in taskId must NOT read another step's log
    const mismatch = readTaskLogSafe(tmp, childCid, 999, { taskId: `${parentCid}/task-248` });
    assert.equal(mismatch.exists, false);

    // writeJsonAtomic retries transient EPERM and succeeds atomically
    const retryTarget = join(tmp, 'catalog-retry.json');
    let transientAttempts = 0;
    writeJsonAtomic(
      retryTarget,
      { ok: true },
      {
        mkdirSync,
        writeFileSync,
        existsSync,
        unlinkSync: (p) => { try { rmSync(p, { force: true }); } catch {} },
        renameSync: (src, dst) => {
          transientAttempts++;
          if (transientAttempts < 3) {
            const err = new Error('EPERM: operation not permitted, rename');
            err.code = 'EPERM';
            throw err;
          }
          renameSync(src, dst);
        },
      },
    );
    assert.equal(transientAttempts, 3);
    assert.deepEqual(JSON.parse(readFileSync(retryTarget, 'utf8')), { ok: true });

    // Persistent EPERM throws and preserves existing valid catalog without truncation
    const lockedTarget = join(tmp, 'catalog-locked.json');
    writeFileSync(lockedTarget, JSON.stringify({ previous: 'valid' }), 'utf8');
    let persistentAttempts = 0;
    assert.throws(() => {
      writeJsonAtomic(
        lockedTarget,
        { next: 'value' },
        {
          mkdirSync,
          writeFileSync,
          existsSync,
          unlinkSync: (p) => { try { rmSync(p, { force: true }); } catch {} },
          renameSync: () => {
            persistentAttempts++;
            const err = new Error('EPERM: operation not permitted, rename');
            err.code = 'EPERM';
            throw err;
          },
        },
      );
    }, /EPERM/);
    assert.equal(persistentAttempts, 3);
    assert.deepEqual(JSON.parse(readFileSync(lockedTarget, 'utf8')), { previous: 'valid' });

    // >32 KiB UTF-16LE BOM log file preserves BOM detection from file header on truncated tail
    const utf16BigCid = '11111111-1111-1111-1111-111111111111';
    const utf16Tasks = join(tmp, 'antigravity', 'brain', utf16BigCid, '.system_generated', 'tasks');
    mkdirSync(utf16Tasks, { recursive: true });
    const bigUtf16Lines = '前缀中文输出行\r\n'.repeat(2500) + '尾部最终输出行\r\n';
    writeFileSync(
      join(utf16Tasks, 'task-9.log'),
      Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(bigUtf16Lines, 'utf16le')]),
    );
    const bigRes = readTaskLogSafe(tmp, utf16BigCid, 9);
    assert.equal(bigRes.exists, true);
    assert.equal(bigRes.truncated, true);
    assert.ok(bigRes.content.includes('尾部最终输出行'));

    // Strict input validation rejects non-string UUID arrays and out-of-range stepIdx
    assert.equal(readTaskLogSafe(tmp, [utf16BigCid], 9).error, 'invalid-params');
    assert.equal(readTaskLogSafe(tmp, utf16BigCid, -1).error, 'invalid-params');
    assert.equal(readTaskLogSafe(tmp, utf16BigCid, 9_999_999).error, 'invalid-params');
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }

  // resolveAppPaths & IPv6 loopback bracket + mapped IPv4 normalization
  const sidecarPaths = resolveAppPaths({
    ANTIGRAVITY_EXECUTABLE_DATA_DIR: join('D:', 'CustomRoot', 'antigravity-insider', 'sidecar_data', 'antigravity-status-bar', 'context', 'data'),
  }, '/unused');
  assert.equal(sidecarPaths.appDirName, 'antigravity-insider');
  assert.equal(normalizeLsHostname('[::1]'), '::1');
  assert.equal(isLoopbackHostname('[::1]'), true);
  assert.equal(isLoopbackHostname('[::ffff:127.0.0.1]'), true);
  assert.equal(isLoopbackHostname('[::ffff:7f00:1]'), true);
  assert.equal(isLoopbackHostname('example.com'), false);
});

test('probePython pins resolved executable path and buildChildEnv deduplicates Path keys', async () => {
  const pinned = await probePython(
    [['py', ['-3']]],
    async () => 'C:\\Program Files\\Python312\\python.exe\r\n',
  );
  assert.deepEqual(pinned, { cmd: 'C:\\Program Files\\Python312\\python.exe', pre: [] });

  const mergedEnv = buildChildEnv(
    'python',
    { Path: '"C:\\Tools\\bin"', PATH: 'C:\\Windows\\system32' },
    'win32',
  );
  const pathKeys = Object.keys(mergedEnv).filter((k) => k.toLowerCase() === 'path');
  assert.deepEqual(pathKeys, ['Path']);
  assert.equal(mergedEnv.Path, 'C:\\Tools\\bin;C:\\Windows\\system32');

  // Relative registry values must be rejected
  const badReg = [
    'HKEY_CURRENT_USER\\Software\\Python\\PythonCore\\3.12\\InstallPath',
    '    ExecutablePath    REG_SZ    relative\\python.exe',
  ].join('\r\n');
  assert.deepEqual(parseWindowsRegistryPythonOutput(badReg), []);
});
