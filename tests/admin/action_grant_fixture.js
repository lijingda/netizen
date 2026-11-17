// The expected keys are injected by ui_harness.py from the actual Web grant plan.
// Keep this literal independent: a server protocol change must fail these fixtures.
function fixtureAction(actionKind, { resource, targetId, scopeKey = null,
  csrfToken = "fixture-csrf", actionToken = "fixture-action" }) {
  const grant = { actionKind, csrfToken, actionToken, target: { resource, targetId, scopeKey } };
  assert.deepEqual(Object.keys(grant).sort(), actionGrantContract.grant, "Web action grant keys changed");
  assert.deepEqual(Object.keys(grant.target).sort(), actionGrantContract.target, "Web action target keys changed");
  assert.equal(typeof actionKind, "string");
  assert.equal(typeof resource, "string");
  assert.equal(typeof targetId, "string");
  assert(scopeKey === null || typeof scopeKey === "string");
  return grant;
}

// POSTs forward authorization, while actionKind selects the action/endpoint.
function fixtureActionPayload(grant) {
  return { csrfToken: grant.csrfToken, actionToken: grant.actionToken, target: grant.target };
}
