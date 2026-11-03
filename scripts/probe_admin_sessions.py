"""Disposable native acceptance for Admin search and latest-answer reads.

Uses only temporary Codex state, the bundled App Server, and a local scripted
Responses provider. It never reads registered/production sessions or uses a real
model account. Legacy creation uses the same fixture-only gap as probe_thread_history.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import importlib.metadata
import json
from pathlib import Path
import sys
import time
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import ThreadStartResponse

from netizen_cli.bindings import BindingStore
from netizen_cli.codex_runtime import CodexRuntime
from netizen_cli.terminal_cleanup import PinnedExperimentalTerminalCleanup
from netizen_cli.thread_history import AppServerThreadHistory
from probe_sdk_partial_answers import isolated_config, message_events, mock_provider, response_events


def runtime_for(codex, store):
    return CodexRuntime(codex=codex, bindings=store,
                        terminal_cleanup=PinnedExperimentalTerminalCleanup(codex),
                        automatic_thread_naming=False)


async def native_case(root: Path, mode: str) -> dict:
    replies = [
        (200, response_events(f"{mode}-{index}", *message_events(
            f"{mode}-answer-{index}", f"ANSWER-{mode}-{index}", "final_answer",
        ))) for index in range(2)
    ] + [(200, response_events(f"{mode}-empty"))]
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        config = CodexConfig(cwd=str(cwd), env=env)
        store = BindingStore(root / "channel.sqlite3")
        evidence = {"mode": mode, "snapshots": []}
        try:
            async with AsyncCodex(config) as codex:
                runtime = runtime_for(codex, store)
                try:
                    if mode == "paginated":
                        thread = await codex.thread_start(cwd=str(cwd), approval_mode=ApprovalMode.deny_all,
                                                          sandbox=Sandbox.read_only)
                    else:
                        # Fixture only: the installed public start facade omits historyMode.
                        started = await codex._client.request("thread/start", {
                            "cwd": str(cwd), "approvalPolicy": "never", "sandbox": "read-only", "historyMode": "legacy",
                        }, response_model=ThreadStartResponse)
                        thread = AsyncThread(codex, started.thread.id)
                    actual = (await thread.read()).thread.history_mode
                    assert getattr(actual, "value", actual) == mode
                    try:
                        await AppServerThreadHistory(codex).list_turns(thread.id, limit=1, items_view="full")
                    except JsonRpcError as error:
                        assert error.message == (
                            f"thread {thread.id} is not materialized yet; "
                            "thread/turns/list is unavailable before first user message"
                        )
                        evidence["before_first_message"] = {"class": type(error).__name__, "message": error.message}
                    else:
                        raise AssertionError("zero-input native behavior changed")
                    empty = await runtime.latest_thread_answer(thread.id)
                    assert empty.turn_id is None and empty.answer is None
                    evidence["zero_input_projection"] = "no_turn"
                    for index in range(3):
                        turn = await thread.turn(f"MESSAGE-{index}")
                        result = await turn.run()
                        assert result.status.value == "completed"
                        await thread.set_name(f"SdkProbe {mode}")
                        expected = f"ANSWER-{mode}-{index}" if index < 2 else None
                        snapshot = await runtime.latest_thread_answer(thread.id)
                        assert snapshot.turn_id == turn.id and snapshot.status == "completed", snapshot
                        assert snapshot.answer == expected, snapshot
                        assert len(provider.requests) == index + 1, "read started model work"
                        evidence["snapshots"].append({"archived": False, **asdict(snapshot)})
                        if index in (1, 2):
                            await codex.thread_archive(thread.id)
                            archived = await runtime.latest_thread_answer(thread.id)
                            assert archived == snapshot
                            found = await runtime.thread_catalog_page(archived=True, search_term="SdkProbe")
                            assert thread.id in {item.thread_id for item in found.threads}, (mode, index, thread.id, found)
                            wrong_case = await runtime.thread_catalog_page(archived=True, search_term="sdkprobe")
                            assert thread.id not in {item.thread_id for item in wrong_case.threads}
                            active = await runtime.thread_catalog_page(archived=False, search_term="SdkProbe")
                            assert thread.id not in {item.thread_id for item in active.threads}
                            assert len(provider.requests) == index + 1
                            evidence["snapshots"].append({"archived": True, **asdict(archived)})
                            if index == 1:
                                await codex.thread_unarchive(thread.id)
                                thread = await codex.thread_resume(thread.id)  # Fixture setup, separate from all reads.
                    try:
                        await runtime.latest_thread_answer(str(uuid.uuid4()))
                    except JsonRpcError as error:
                        evidence["missing_thread"] = {"class": type(error).__name__, "code": error.code}
                    else:
                        raise AssertionError("missing history was mistaken for no answer")
                finally:
                    await runtime.cancel_tasks()
            # A cold process reads archived history without resuming the Thread.
            async with AsyncCodex(config) as codex:
                runtime = runtime_for(codex, store)
                try:
                    cold = await runtime.latest_thread_answer(thread.id)
                    assert cold.turn_id == snapshot.turn_id and cold.answer is None
                    archived = await runtime.thread_catalog_page(archived=True, search_term="SdkProbe")
                    assert thread.id in {item.thread_id for item in archived.threads}
                    evidence["cold_archived_read_without_resume"] = True
                finally:
                    await runtime.cancel_tasks()
            assert len(provider.requests) == 3 and not provider.errors and not provider.replies
            evidence.update(passed=True, model_requests=3, real_model_used=False,
                            search_case_sensitive=True, no_backward_answer_search=True,
                            archived_membership_unchanged_by_read=True)
            return evidence
        finally:
            store.close()


async def sparse_search_case(root: Path) -> dict:
    """Exercise the real native SQL/catalog path plus this instance's Binding join."""
    from netizen_cli.domain import FeishuScope, ScopeKind
    from netizen_cli.management import InstanceManagementService, ManagementRuntimePort, SessionQuery
    from netizen_cli.management.coordination import ScopeCoordinator
    from netizen_cli.projects import ProjectRegistry

    count = 105  # More than the production native catalog batch of 100.
    replies = [(200, response_events(f"sparse-{index}", *message_events(
        f"sparse-answer-{index}", "FIXTURE-ANSWER", "final_answer",
    ))) for index in range(count)]
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        store = BindingStore(root / "channel.sqlite3")
        projects = ProjectRegistry(store=store, project_root=root, projects={"probe": cwd})
        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                runtime = runtime_for(codex, store)
                management = InstanceManagementService(
                    bindings=store, projects=projects, runtime=ManagementRuntimePort(runtime),
                    scope_coordinator=ScopeCoordinator(), root=root,
                )
                try:
                    for index in range(count):
                        thread = await codex.thread_start(cwd=str(cwd), approval_mode=ApprovalMode.deny_all,
                                                          sandbox=Sandbox.read_only)
                        result = await (await thread.turn(f"SPARSE-FIXTURE-{index}")).run()
                        assert result.status.value == "completed"
                        await thread.set_name(f"SPARSE-SDK-PROBE-{index}")
                    # Select an owned target after the first native batch, without
                    # assuming tie-break ordering for sub-second fixture creation.
                    first = await runtime.thread_catalog_page(archived=False, limit=100, search_term="SPARSE-SDK-PROBE")
                    assert len(first.threads) == 100 and first.next_cursor
                    second = await runtime.thread_catalog_page(archived=False, limit=100, cursor=first.next_cursor,
                                                               search_term="SPARSE-SDK-PROBE")
                    assert len(second.threads) == count - 100 and second.next_cursor is None
                    owned = second.threads[-1].thread_id
                    binding = store.create_channel_binding(scope=FeishuScope("probe-app", "probe-chat", ScopeKind.GROUP),
                                                           project_alias="probe", creator_id="probe-owner")
                    store.assign_native_thread_id(binding.id, owned)
                    native_read = runtime.thread_catalog_page
                    reads = []

                    async def recorded_read(**kwargs):
                        reads.append({"cursor": kwargs.get("cursor"), "search_term": kwargs.get("search_term")})
                        return await native_read(**kwargs)

                    runtime.thread_catalog_page = recorded_read
                    started = time.monotonic()
                    page = await management.query_sessions(query=SessionQuery(search_term="SPARSE-SDK-PROBE"), limit=1,
                                                           deadline=asyncio.get_running_loop().time() + 10)
                    elapsed = time.monotonic() - started
                    assert [item.record.binding.id for item in page.items] == [binding.id]
                    assert len(reads) == 2 and all(read["search_term"] == "SPARSE-SDK-PROBE" for read in reads)
                    assert len(provider.requests) == count and not provider.errors and not provider.replies
                    return {"scenario": "sparse_native_search", "passed": True, "native_candidates": count,
                            "owned_bindings": 1, "native_pages_read": len(reads), "query_seconds": elapsed,
                            "model_requests_during_search": 0, "fixture_model_requests": count,
                            "real_model_used": False, "production_query": "InstanceManagementService.query_sessions"}
                finally:
                    await management.close()
                    await runtime.cancel_tasks()
        finally:
            store.close()


async def run() -> dict:
    evidence = {"sdk": importlib.metadata.version("openai-codex"), "cases": [], "production_history_read": False}
    with tempfile.TemporaryDirectory(prefix="netizen-admin-sessions-") as temporary:
        for mode in ("paginated", "legacy"):
            root = Path(temporary) / mode
            root.mkdir()
            async with asyncio.timeout(90):
                evidence["cases"].append(await native_case(root, mode))
        sparse_root = Path(temporary) / "sparse"
        sparse_root.mkdir()
        async with asyncio.timeout(180):
            evidence["sparse_search"] = await sparse_search_case(sparse_root)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    evidence = asyncio.run(run())
    output = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
