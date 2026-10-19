-- Additional v15 resource metadata; applied after migrating the frozen v14 fixture.
INSERT INTO managed_worktrees(
    worktree_id, project_alias, project_generation, root, cwd, common_dir,
    git_dir, identity_token, origin_binding_id, creation_state, revision, created_at
) VALUES (
    'worktree-shared', 'work', 1, '/tmp/managed/shared', '/tmp/managed/shared/src',
    '/tmp/project/.git', '/tmp/project/.git/worktrees/shared', 'root:1:2/gitdir:3:4',
    'binding', 'ready', 2, '2026-10-19T01:00:00+00:00'
);
UPDATE bindings SET working_cwd='/tmp/managed/shared/src', worktree_id='worktree-shared'
WHERE binding_id='binding';
INSERT INTO bindings(
    binding_id,scope_key,project_alias,creator_id,created_at,activated_at,working_cwd,worktree_id
) VALUES (
    'lazy-worktree-binding','scope','work','user','2026-10-19','2026-10-19',
    '/tmp/managed/shared/src','worktree-shared'
);
INSERT INTO managed_worktrees(
    worktree_id, project_alias, project_generation, root, cwd, common_dir,
    creation_state, created_at
) VALUES (
    'worktree-unknown', 'removed-project', 2, '/tmp/managed/unknown',
    '/tmp/managed/unknown', '/tmp/removed-project/.git', 'unknown',
    '2026-10-19T01:01:00+00:00'
);
