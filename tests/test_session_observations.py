from unittest.mock import patch
import unittest



from netizen_cli.domain import FeishuScope, ScopeKind
from netizen_cli.session_views import Availability
from netizen_cli.runtime.contracts import ContextWindowUsage
from tests.support.channel_fixtures import channel_fixture




class ObservationTest(unittest.IsolatedAsyncioTestCase):


    async def test_codex_status_preserves_context_sample_and_model_read_times(self):
        async with channel_fixture() as fx:
            scope = FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
            binding = (await fx.create_binding(scope)).binding
            with patch.object(fx.runtime, "context_window_usage", side_effect=AssertionError("lazy read")):
                self.assertIs(fx.app._context_usage_field(binding, active_turn=False).availability, Availability.NOT_APPLICABLE)
            fx.runtime.materialize(binding.id, "native", cwd=fx.project)
            binding = fx.store.get(binding.id)
            self.assertIs(fx.app._context_usage_field(binding, active_turn=False).availability, Availability.UNOBSERVED)
            with patch.object(fx.runtime, "context_window_read_failed_at", return_value=12.5):
                failed = fx.app._context_usage_field(binding, active_turn=False)
                self.assertEqual((failed.availability, failed.sampled_at), (Availability.UNAVAILABLE, 12.5))
            fx.runtime.context_window_usage_values[binding.id] = ContextWindowUsage(7, 100, 123)
            for active in (True, False):
                field = fx.app._context_usage_field(binding, active_turn=active)
                self.assertEqual((field.sampled_at, field.source, field.usage.used_tokens), (123, "Codex token usage", 7))
            with patch.object(fx.runtime, "context_window_usage", side_effect=OSError), self.assertRaises(OSError):
                fx.app._context_usage_field(binding, active_turn=False)
            model = await fx.app._binding_model_status(binding)
            for field in (model.model, model.effort, model.speed, model.configuration):
                self.assertIsNotNone(field.sampled_at)
                self.assertEqual(field.source, "Binding settings")
            self.assertIs(model.model.availability, Availability.UNOBSERVED)
            self.assertIs(model.configuration.availability, Availability.OBSERVED)
