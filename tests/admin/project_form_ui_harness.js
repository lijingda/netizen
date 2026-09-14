const state = { projects: { actions: { register: {}, createDirectory: {} } } };
const requests = [];
let completeMutation;
let failMutation;
function mutate(path, envelope, payload) {
  requests.push({ path, envelope, payload });
  return new Promise((resolve, reject) => {
    completeMutation = resolve;
    failMutation = reject;
  });
}
FormData.prototype.get = function (name) {
  return this.entries.find(([key]) => key === name)?.[1] ?? null;
};
// SHIPPED_PROJECT_FORM_HANDLERS
(async () => {
  for (const [selector, action, endpoint, path] of [
    ["#register-project", "register", "/api/v1/projects/register", "/workspace/existing"],
    ["#create-project", "createDirectory", "/api/v1/projects/create-directory", ""],
  ]) {
    const form = document.querySelector(selector);
    const aliasInput = form.querySelectorAll("input").find((input) => input.name === "alias");
    const pathInput = form.querySelectorAll("input").find((input) => input.name === "path");
    const submit = form.listeners.get("submit")[0];
    for (const succeeds of [true, false]) {
      aliasInput.value = "example";
      pathInput.value = path;
      const event = { currentTarget: form, prevented: false,
        preventDefault() { this.prevented = true; } };
      const requestCount = requests.length;
      const pending = submit(event);
      // Browser event dispatch ends while the network request is still pending.
      event.currentTarget = null;
      assert(event.prevented);
      assert.equal(requests.length, requestCount + 1);
      assert.deepEqual(requests.at(-1), {
        path: endpoint,
        envelope: state.projects.actions[action],
        payload: { alias: "example", path: path || null },
      });
      assert.equal(aliasInput.value, "example");
      assert.equal(pathInput.value, path);
      if (succeeds) {
        completeMutation({ message: "created" });
        await pending;
        assert.equal(aliasInput.value, "");
        assert.equal(pathInput.value, "");
      } else {
        const error = new TypeError("network unavailable");
        failMutation(error);
        await assert.rejects(pending, (caught) => caught === error);
        assert.equal(aliasInput.value, "example");
        assert.equal(pathInput.value, path);
      }
      assert.equal(requests.length, requestCount + 1);
    }
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
