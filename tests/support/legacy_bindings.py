from netizen_cli.bindings import BindingStore, ThreadBinding
from netizen_cli.domain import FeishuScope


def legacy_admin_lazy_binding(
    store: BindingStore, *, scope: FeishuScope, project_alias: str,
    expected_project_revision: int,
) -> ThreadBinding:
    """Seed the never-activated state left by the removed Admin create action."""
    previous = store.get_scope(scope.key)
    binding = store.create_channel_binding(
        scope=scope, project_alias=project_alias, creator_id="admin:web",
        expected_project_revision=expected_project_revision,
    )
    store._connection.execute(
        "UPDATE scopes SET active_binding_id = ?, updated_at = ? WHERE scope_key = ?",
        (previous.active_binding_id, previous.updated_at, scope.key),
    )
    store._connection.execute(
        "UPDATE bindings SET ever_activated = 0 WHERE binding_id = ?",
        (binding.id,),
    )
    return store.get(binding.id)
