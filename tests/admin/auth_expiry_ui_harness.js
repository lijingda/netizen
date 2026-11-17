const originalFetch = fetch;
const heldReads = [];
let holdReads = false;
let fetchCalls = 0;
document.body.classList.toggle = (name, force) => {
  const classes = new Set(document.body.className.split(" ").filter(Boolean));
  const present = force === undefined ? !classes.has(name) : force;
  if (present) classes.add(name); else classes.delete(name);
  document.body.className = [...classes].join(" ");
  return present;
};
fetch = async (path, options = {}) => {
  fetchCalls += 1;
  if (holdReads && !path.startsWith('/api/v1/sessions?') && !path.startsWith('/api/v1/projects/options?')) {
    const gate = deferred();
    heldReads.push(gate);
    await gate.promise;
    return { ok: true, status: 200, json: async () => ({}) };
  }
  return originalFetch(path, options);
};
document.createDocumentFragment = () => new Element('fragment');
// SHIPPED_ADMIN_PAGE
(async () => {
  await flush();
  ordinary.actions = { configure: grant('configure'), rename: grant('rename'), archive: grant('archive') };
  ordinary.canReadLatestAnswer = true;
  Object.assign(ordinary.sessionSettings, { reaction_pulse_enabled: false, progress_card_enabled: false,
    completion_mention_enabled: false, progress_card_settings: null });
  state.projects.actions = { register: grant('register') };
  state.defaults = { chat: { actions: { create: grant('default') } } };
  state.schedules = { actions: { create: grant('schedule') } };
  const trigger = document.querySelector('#navigation-open');
  const openings = {
    'project-delete-dialog': () => confirmProjectDelete({ alias: 'netizen' }, grant('delete')),
    'project-drawer': () => openProjectEditor(),
    'file-browser-drawer': () => window.NetizenProjectFiles.open({ alias: 'netizen', cwd: '/project' }),
    'worktree-create-drawer': () => document.querySelector('#worktree-new').click(),
    'worktree-settings-drawer': () => document.querySelector('#worktree-configure').click(),
    'session-rename-dialog': () => openSessionRenameEditor(ordinary, trigger),
    'session-answer-drawer': () => openConversation(ordinary),
    'session-settings-drawer': () => openSessionSettingsEditor(ordinary, trigger),
    'defaults-drawer': () => openDefaultEditor('chat'),
    'schedule-detail': () => showSchedule('plan-one', trigger),
    'schedule-drawer': () => openScheduleEditor(),
    'navigation-drawer': () => trigger.click(),
    'session-batch-dialog': async () => {
      state.tab = 'sessions';
      await loadSessions();
      sessionSelection.ids.add(ordinary.bindingId);
      renderSessionSelection();
      return runSessionBatch('archive', trigger);
    },
    'admin-confirm': () => {
      confirmResponder = null;
      return window.NetizenConfirm.ask({ body: '确认操作' });
    },
  };
  // Enumerate the shipped page, so adding a dialog requires an opening scenario.
  for (const dialog of document.querySelectorAll('dialog')) {
    assert(openings[dialog.id], `Missing normal opening scenario for ${dialog.id}`);
  }
  const dialog = document.querySelector(`#${testDialog}`);
  assert(dialog, testDialog);
  holdReads = true;
  const opening = openings[testDialog]();
  await flush();
  assert(dialog.open, `${testDialog} did not open through its normal entry`);
  const settings = sessionSettingsEditor, defaults = defaultsEditor, schedule = scheduleEditor;
  // Expiry must bypass both dirty and saving restrictions while reads are pending.
  if (settings) { settings.settings.reaction_pulse_enabled = true; settings.saving = true; }
  if (defaults) { defaultsInput('chat').value = 'oc_draft'; defaultsBusy = true; }
  if (schedule) { scheduleInput('name').value = '计划草稿'; scheduleInput('fields').disabled = true; }
  const beforeExpiry = confirmations.length;
  expireAdminSession();
  assert.equal(confirmations.length, beforeExpiry, 'expiry must not ask to discard a draft');
  const assertClosed = () => {
    assert.deepEqual(document.querySelectorAll('dialog').filter((node) => node.open).map((node) => node.id), []);
    for (const name of ['workspace-drawer-open', 'project-delete-open', 'session-answer-open',
      'session-settings-drawer-open', 'defaults-drawer-open', 'schedule-drawer-open', 'schedule-detail-open',
      'navigation-open', 'session-batch-open', 'file-browser-open', 'admin-confirm-open']) {
      assert(!document.body.className.split(' ').includes(name), name);
    }
    assert.equal(document.querySelector('#auth-expired').hidden, false);
  };
  assertClosed();
  for (const editor of [settings, defaults, schedule]) if (editor) assert.equal(editor.action, null);
  const requestsAtExpiry = fetchCalls;
  for (const gate of heldReads) gate.resolve();
  await opening;
  await flush();
  assertClosed();
  assert.equal(sessionSettingsEditor, null);
  assert.equal(defaultsEditor, null);
  assert.equal(scheduleEditor, null);
  for (const id of ['session-settings-editor', 'defaults-editor', 'schedule-editor']) {
    document.querySelector(`#${id}`).dispatch('submit');
  }
  await flush();
  assert.equal(fetchCalls, requestsAtExpiry, 'expired reads/actions must never submit or chain another request');
  console.log(`passed ${testDialog}`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
