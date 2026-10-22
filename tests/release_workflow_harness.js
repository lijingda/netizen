const assert = require('node:assert/strict');
const fs = require('node:fs');
const [script, stage, tagKind, change] = process.argv.slice(1);
const run = new (Object.getPrototypeOf(async function() {}).constructor)(
  'github', 'context', 'core', 'require', script);
const operations = [];
const tagReads = [];
const outputs = {};
const commit = { type: 'commit', sha: change === 'moved' ? 'b'.repeat(40) : process.env.RELEASE_COMMIT };
const release = { id: 42, html_url: 'https://example.test/release', draft: true, tag_name: process.env.RELEASE_TAG };
const github = { rest: {
  git: {
    getRef: async (args) => {
      assert.deepEqual(args, { owner: 'owner', repo: 'repo', ref: 'tags/v1.2.3' });
      tagReads.push('ref');
      return { data: { object: tagKind === 'annotated' ? { type: 'tag', sha: 'annotated-tag' } : commit } };
    },
    getTag: async (args) => {
      assert.deepEqual(args, { owner: 'owner', repo: 'repo', tag_sha: 'annotated-tag' });
      tagReads.push('tag');
      return { data: { object: commit } };
    },
  },
  repos: {
    getReleaseByTag: async () => { operations.push('getReleaseByTag'); throw Object.assign(new Error('absent'), { status: 404 }); },
    createRelease: async (args) => {
      operations.push('createRelease');
      assert.equal(args.target_commitish, process.env.RELEASE_COMMIT);
      assert.equal(args.draft, true);
      assert(args.body.includes(process.env.MAIN_GATE_URL));
      assert(args.body.includes(process.env.MANIFEST_SHA256));
      return { data: release };
    },
    uploadReleaseAsset: async (args) => {
      operations.push(`upload:${args.name}`);
      assert.equal(args.release_id, release.id);
      assert.deepEqual(args.data, fs.readFileSync(`dist/${args.name}`));
    },
    getRelease: async () => { operations.push('getRelease'); return { data: release }; },
    updateRelease: async (args) => {
      operations.push('updateRelease');
      assert.equal(args.release_id, release.id);
      assert.equal(args.draft, false);
      return { data: release };
    },
  },
} };
const trackedFs = { ...fs };
for (const name of ['readFileSync', 'mkdirSync', 'writeFileSync']) {
  trackedFs[name] = (...args) => { operations.push(`${name}:${args[0]}`); return fs[name](...args); };
}
const core = { setOutput: (key, value) => { outputs[key] = value; }, notice() {} };
(async () => {
  const attempt = run(github, { repo: { owner: 'owner', repo: 'repo' } }, core,
    (name) => name === 'fs' ? trackedFs : require(name));
  if (change === 'moved') {
    await assert.rejects(attempt, /[Tt]ag .*moved/);
    assert.deepEqual(operations, [], 'reject before artifact preparation or release API access');
    assert.deepEqual(outputs, {});
    if (stage === 'publish-pypi') assert(!fs.existsSync('dist'));
  } else {
    await attempt;
    if (stage === 'prepare-release') {
      assert.equal(outputs['release-id'], 42);
      assert.deepEqual(operations.filter((operation) => operation.startsWith('upload:')), [
        'upload:netizen_cli-1.2.3-py3-none-any.whl', 'upload:netizen-cli-release.json',
      ]);
    } else if (stage === 'publish-pypi') {
      assert.deepEqual(fs.readdirSync('dist'), ['netizen_cli-1.2.3-py3-none-any.whl']);
      assert.deepEqual(fs.readFileSync('dist/netizen_cli-1.2.3-py3-none-any.whl'),
        fs.readFileSync('candidate/netizen_cli-1.2.3-py3-none-any.whl'));
    } else {
      assert.deepEqual(operations, ['getRelease', 'updateRelease']);
    }
  }
  assert.deepEqual(tagReads, tagKind === 'annotated' ? ['ref', 'tag'] : ['ref']);
})().catch((error) => { console.error(error); process.exitCode = 1; });
