import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


from netizen_cli.backends import BackendError, instance_backend
from netizen_cli.main import ServiceCore


class BackendFactoryTest(unittest.IsolatedAsyncioTestCase):
    def core(self, backend):
        return ServiceCore(settings=SimpleNamespace(backend=backend, app_id="app"),
                           channel=Mock(), store=Mock(), projects=Mock(), instance_root=Path("/unused"))

    async def test_unconfigured_root_selects_codex_and_unknown_selection_cannot_bind(self):
        core = self.core(instance_backend({}))
        self.assertEqual(core._backend_factory(SimpleNamespace(has_admins=True)), core._assemble_codex)
        core = self.core("unavailable")
        with (patch("netizen_cli.main.AdminUsers"), patch("netizen_cli.main.AdminWebRunner") as listener,
              patch("netizen_cli.main.AsyncCodex") as codex, self.assertRaises(BackendError)):
            await core.start()
        listener.assert_not_called()
        codex.assert_not_called()
