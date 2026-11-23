import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  collectDependencyStamps,
  dependencyStampsMatch,
  isLoopbackHostname,
  normalizeCatalogResponse,
  probePython,
  readTaskLogSafe,
  snapshotDependencyStamps,
  stampsUnchanged,
  workspacePath,
  writeJsonAtomic,
} from '../sidecars/context/sidecar_utils.mjs';

test('workspacePath handles local, localhost, Windows, and encoded paths', () => {
  assert.equal(workspacePath(''), '');
  assert.equal(workspacePath('https://example.com/ws'), '');
  if (process.platform === 'win32') {
    assert.equal(workspacePath('file:///C:/my%20repo'), 'C:\\my repo');
    assert.equal(workspacePath('file://localhost/C:/repo'), 'C:\\repo');
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

    if (process.platform !== 'win32') {
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
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});
