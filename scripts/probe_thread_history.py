"""Disposable acceptance for the production read-only history adapter.

Runs the bundled server with a local mock model in temporary Codex directories.
No production account, registered sessions, or Feishu messages are accessed.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from netizen_cli.thread_history import AppServerThreadHistory
from probe_sdk_partial_answers import isolated_config, mock_provider, message_events, response_events
from openai_codex import AsyncCodex, AsyncThread, CodexConfig, ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ThreadStartResponse
from openai_codex.errors import JsonRpcError, MethodNotFoundError
import openai_codex

async def native_case(root, mode):
    replies = [(200, response_events(f'{mode}-{i}', *message_events(f'{mode}-answer-{i}', f'ANSWER-{mode}-{i}', 'final_answer'))) for i in range(3)]
    with mock_provider(replies) as provider:
        cwd, env = isolated_config(root, provider)
        env.pop('HOME', None)
        config = CodexConfig(cwd=str(cwd), env=env)
        async with AsyncCodex(config) as codex:
            if mode == 'public_default':
                thread = await codex.thread_start(cwd=str(cwd), approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only)
            else:
                # Test fixture only: installed Python ThreadStartParams omits historyMode.
                started = await codex._client.request('thread/start', {'cwd':str(cwd),'approvalPolicy':'never','sandbox':'read-only','historyMode':'legacy'}, response_model=ThreadStartResponse)
                thread = AsyncThread(codex, started.thread.id)
            meta = (await thread.read()).thread
            actual_mode = getattr(meta.history_mode, 'value', meta.history_mode)
            assert actual_mode == ('paginated' if mode == 'public_default' else 'legacy'), actual_mode
            turn_ids = []
            for i in range(3):
                handle = await thread.turn(f'USER-{mode}-{i}')
                result = await handle.run()
                assert result.status.value == 'completed'
                turn_ids.append(handle.id)
            bridge = AppServerThreadHistory(codex)
            # Newest-first native pages, with real opaque continuation cursors.
            got = []; cursor = None; pages = []; cursors = []
            while True:
                page = await bridge.list_turns(thread.id, cursor=cursor)
                assert len(page.data) == 1
                got.extend(t.id for t in page.data)
                assert page.data[0].items_view.value == 'full'
                pages.append({'ids':[t.id for t in page.data], 'item_count':len(page.data[0].items), 'next_present':page.next_cursor is not None})
                cursor = page.next_cursor
                if cursor is None: break
                assert cursor not in cursors
                cursors.append(cursor)
            assert got == list(reversed(turn_ids)), (got, turn_ids)
            summary = await bridge.list_turns(thread.id, limit=1, items_view='summary')
            assert summary.data[0].id == turn_ids[-1]
            item_error = None
            try:
                newest = await bridge.list_items(thread.id, turn_ids[-1], limit=20)
            except MethodNotFoundError as error:
                assert actual_mode == 'legacy'
                item_error = {'class':type(error).__name__, 'code':error.code, 'message':error.message}
                latest_full = await bridge.list_turns(thread.id, limit=1, items_view='full')
                assert latest_full.data[0].id == turn_ids[-1]
                latest_item_types = [x.root.type for x in latest_full.data[0].items]
                item_uniqueness_verified = None
            else:
                assert newest.data and all(x.turn_id == turn_ids[-1] for x in newest.data)
                assert len({x.item.root.id for x in newest.data}) == len(newest.data)
                latest_item_types = [x.item.root.type for x in newest.data]
                item_uniqueness_verified = True
            invalid = None
            try: await bridge.list_turns(thread.id, cursor='invalid-history-cursor')
            except JsonRpcError as error: invalid = {'class':type(error).__name__, 'code':error.code}
            assert invalid is not None
            # Same connection remains healthy after the invalid read.
            after_error = await bridge.list_turns(thread.id, items_view='summary')
            assert after_error.data[0].id == turn_ids[-1]
            await codex.thread_archive(thread.id)
            archived = await bridge.list_turns(thread.id, items_view='summary')
            assert archived.data[0].id == turn_ids[-1]
            assert len(provider.requests) == 3 and not provider.errors and not provider.replies
            return {'creation':mode, 'mode':actual_mode, 'turn_count':3, 'pages':pages, 'turn_cursor_is_turn_id':[x in turn_ids for x in cursors], 'latest_summary_item_types':[x.root.type for x in summary.data[0].items], 'latest_items_types':latest_item_types, 'items_api_error':item_error, 'latest_items_unique':item_uniqueness_verified, 'invalid_cursor':invalid, 'connection_reused_after_error':True, 'archived_read':True, 'model_requests':len(provider.requests), 'production_adapter':'AppServerThreadHistory'}

async def main():
    data = {'sdk':openai_codex.__version__, 'cases':[]}
    with tempfile.TemporaryDirectory(prefix='netizen-history-adapter-') as tmp:
        root = Path(tmp)
        for mode in ('public_default','legacy'):
            case_root = root / mode; case_root.mkdir()
            async with asyncio.timeout(90): data['cases'].append(await native_case(case_root, mode))
            print(json.dumps(data['cases'][-1]), flush=True)
    # Print the disposable acceptance evidence; never write instance state.
    print(json.dumps(data, indent=2))
if __name__ == "__main__":
    asyncio.run(main())
