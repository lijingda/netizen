# Netizen local operator profile

Copy this file to `LOCAL_ENVIRONMENT.md` when one checkout needs to remember
developer-specific or deployment-specific coordinates:

```bash
cp LOCAL_ENVIRONMENT.example.md LOCAL_ENVIRONMENT.md
chmod 600 LOCAL_ENVIRONMENT.md
```

`LOCAL_ENVIRONMENT.md` is intentionally ignored by Git. It is an operator
notebook, not a Netizen runtime configuration file. A clean clone does not
need it: public development and deployment instructions remain in `README.md`,
`config.example.yaml`, and `docs/deployment.md`.

Never place raw Feishu App Secrets, Admin Web credentials, Codex credentials,
cookies, CSRF tokens, or bearer tokens in this file. Record protected file
paths only.

## Development workstation

- Checkout: `<absolute-checkout-path>`
- Account home: `<absolute-account-home>`
- Shell: `<absolute-login-shell>`
- Codex home: `<absolute-codex-home>`
- Local runtime config: `<absolute-config-path-or-not-configured>`
- Feishu credential path: `<absolute-protected-file-path-or-not-configured>`
- Admin credential path: `<absolute-protected-file-path-or-not-configured>`

## Deployment target

- SSH target: `<ssh-config-alias-or-user-at-host>`
- Expected endpoint: `<expected-user>@<server-address>:<port>`
- Account home: `<absolute-remote-account-home>`
- Login shell: `<absolute-remote-login-shell>`
- Product root: `<remote-account-home>/.netizen`
- Codex home: `<absolute-remote-codex-home>`
- Probe cwd: `<absolute-test-project-cwd>`
- Admin Web: `http://<server-address>:<port>`
- Runtime config: `<remote-account-home>/.netizen/config.yaml`
- Feishu credential path:
  `<remote-account-home>/.netizen/credentials/feishu-app-secret`
- Admin credential path:
  `<remote-account-home>/.netizen/credentials/admin-web-secret`

### Retired or forbidden targets

- `<target-that-must-not-receive-deployments>`

## Operator notes

- Verify the expanded SSH hostname and user before every deployment.
- Run live probes in the same account login environment used by the service.
- Keep instance-specific release results, PIDs, native IDs, database counts,
  backup paths, and private-network observations here instead of tracked
  documentation.
- Do not make installer, service launcher, systemd, or production runtime read
  this file.
