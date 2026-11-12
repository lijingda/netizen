"""Disposable native acceptance for Admin search and read-only conversation pages.

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
import threading
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


async def conversation_case(root: Path, mode: str) -> dict:
    """Cover the production summary projection against both native stores."""
    replies = [(200, response_events(f"page-{index}", *message_events(
        f"answer-{index}", f"ANSWER-{index}", "final_answer",
    ))) for index in range(10)]
    replies += [
        (200, response_events("steer-first", *message_events("steer-initial", "STEER-FIRST-ANSWER", "final_answer"))),
        (200, response_events("steer-final", *message_events("steer-answer", "STEER-FINAL-ANSWER", "final_answer"))),
        (400, b'{"error":{"message":"CONVERSATION-FAILURE","type":"invalid_request_error"}}'),
        (200, response_events("interrupted", *message_events("never-final", "INTERRUPTED-ANSWER", "final_answer"))),
    ]
    loop = asyncio.get_running_loop()

    class ArrivalGate(threading.Event):
        def __init__(self):
            super().__init__()
            self.arrived = asyncio.Event()

        def wait(self, timeout=None):
            loop.call_soon_threadsafe(self.arrived.set)
            return super().wait(timeout)

    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        store = BindingStore(root / "channel.sqlite3")
        gate, interrupt_gate = ArrivalGate(), ArrivalGate()
        provider.response_gates[11] = gate
        provider.response_gates[14] = interrupt_gate
        try:
            async with AsyncCodex(CodexConfig(cwd=str(cwd), env=env)) as codex:
                runtime = runtime_for(codex, store)
                try:
                    if mode == "paginated":
                        thread = await codex.thread_start(cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only)
                    else:
                        started = await codex._client.request("thread/start", {
                            "cwd": str(cwd), "approvalPolicy": "never", "sandbox": "read-only", "historyMode": "legacy",
                        }, response_model=ThreadStartResponse)
                        thread = AsyncThread(codex, started.thread.id)
                    turn_ids = []
                    for index in range(10):
                        turn = await thread.turn(f"FIRST-{index}")
                        result = await turn.run()
                        assert result.status.value == "completed"
                        turn_ids.append(turn.id)
                    turn = await thread.turn("STEER-FIRST")
                    turn_ids.append(turn.id)
                    await asyncio.wait_for(gate.arrived.wait(), 10)
                    running_summary = await AppServerThreadHistory(codex).list_turns(thread.id, limit=10, items_view="summary")
                    first_user = next(item.root for item in running_summary.data[0].items if item.root.type == "userMessage")
                    assert [part.root.text for part in first_user.content if part.root.type == "text"] == ["STEER-FIRST"]
                    running_page = await runtime.thread_conversation(thread.id)
                    running_turn = running_page.turns[0]
                    assert running_turn.turn_id == turn.id and running_turn.status == "inProgress"
                    assert [part.text for part in running_turn.inputs] == ["STEER-FIRST"]
                    assert running_turn.answer is None and not running_turn.images
                    assert len(provider.requests) == 11, "running history read started model work"
                    await turn.steer("STEER-SECOND")
                    gate.set()
                    result = await turn.run()
                    assert result.status.value == "completed", result
                    full = await AppServerThreadHistory(codex).list_turns(thread.id, limit=1, items_view="full")
                    assert len([item for item in full.data[0].items if item.root.type == "userMessage"]) == 2
                    failed = await thread.turn("FAIL-FIRST")
                    turn_ids.append(failed.id)
                    try:
                        await failed.run()
                    except RuntimeError as error:
                        assert "CONVERSATION-FAILURE" in str(error), error
                    else:
                        raise AssertionError("failed native Turn did not raise its provider error")
                    interrupted = await thread.turn("INTERRUPT-FIRST")
                    turn_ids.append(interrupted.id)
                    await asyncio.wait_for(interrupt_gate.arrived.wait(), 10)
                    await interrupted.interrupt()
                    interrupted_result = await interrupted.run()
                    interrupt_gate.set()
                    assert interrupted_result.status.value == "interrupted", interrupted_result
                    requests_before = len(provider.requests)
                    all_turns, pages, cursors = [], [], set()
                    cursor = None
                    for _ in range(4):
                        page = await runtime.thread_conversation(thread.id, cursor=cursor)
                        all_turns.extend(page.turns)
                        pages.append({"count": len(page.turns), "has_next": page.next_cursor is not None})
                        cursor = page.next_cursor
                        if cursor is None:
                            break
                        assert cursor not in cursors
                        cursors.add(cursor)
                    else:
                        raise AssertionError("summary cursor chain did not terminate")
                    assert [turn.turn_id for turn in all_turns] == list(reversed(turn_ids))
                    assert [page["count"] for page in pages] == [10, 3]
                    assert all_turns[0].status == "interrupted" and all_turns[0].answer is None
                    assert all_turns[1].status == "failed" and all_turns[1].error
                    assert [part.text for part in all_turns[2].inputs] == ["STEER-FIRST"]
                    invalid = None
                    try:
                        await runtime.thread_conversation(thread.id, cursor="invalid-history-cursor")
                    except JsonRpcError as error:
                        invalid = {"class": type(error).__name__, "code": error.code, "message": error.message}
                    assert invalid is not None
                    assert invalid["class"] == "InvalidRequestError" and invalid["code"] == -32600
                    assert "cursor" in invalid["message"].lower()
                    after_error = await runtime.thread_conversation(thread.id)
                    assert after_error.turns[0].turn_id == interrupted.id
                    await codex.thread_archive(thread.id)
                    archived = await runtime.thread_conversation(thread.id)
                    assert archived == after_error
                    archived_catalog = await runtime.thread_catalog_page(archived=True)
                    assert thread.id in {item.thread_id for item in archived_catalog.threads}
                    assert len(provider.requests) == requests_before, "history reads must not run model work"
                    assert not provider.replies
                    assert all(error == "model response connection closed early" for error in provider.errors), provider.errors
                    return {"mode": mode, "passed": True, "pages": pages, "turn_count": len(all_turns),
                            "complete_newest_to_oldest_chain": True, "steer_first_input_only": True,
                            "running_summary_first_input": True, "running_projected_first_input": True,
                            "failed_error": all_turns[1].error, "interrupted_without_final": True,
                            "invalid_cursor": invalid, "connection_reused_after_error": True,
                            "archived_read_without_resume": True, "model_requests_during_reads": 0,
                            "fixture_model_requests": requests_before, "real_model_used": False}
                finally:
                    gate.set()
                    interrupt_gate.set()
                    await runtime.cancel_tasks()
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
            conversation_root = Path(temporary) / f"conversation-{mode}"
            conversation_root.mkdir()
            async with asyncio.timeout(90):
                evidence.setdefault("conversations", []).append(await conversation_case(conversation_root, mode))
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
