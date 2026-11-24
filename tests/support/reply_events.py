"""Construct exact Codex presentation events for synthetic Channel tests."""

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.channel.codex_reply_events import CodexReplyEvents
from netizen_cli.channel.reply_events import PresentationKey, TurnResult
from netizen_cli.file_evidence import FileEvidence
from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
from netizen_cli.runtime.codex_replies import ordinary_conclusion
from netizen_cli.runtime.contracts import TurnOutcome


def codex_result(outcome):
    if isinstance(outcome, TurnOutcome):
        outcome = ordinary_conclusion(outcome)
    return CodexReplyEvents.result(outcome)


def terminal_answers(target, items, *, goal=False):
    return TurnResult(
        target=target, status="completed", content="", final_response=None,
        task_feedback=BindingTaskFeedback(), cwd=None, activity=None,
        partials=tuple(target.partial(item, presentation_id=target.key.turn_id if goal else None) for item in items),
        file_evidence=FileEvidence, file_binding_id=target.key.owner_id, physical_turn_id=target.key.turn_id,
        allow_suppression=False, run_continues=False, end=None,
    )


async def open_card(
    presenter,
    runtime,
    *,
    thread_id,
    turn_id,
    origin,
    binding_id=None,
    side_id=None,
    reply=None,
    reply_partial=None,
    validate_reply=None,
    progress_enabled=True,
    logical_turn=False,
):
    key = PresentationKey(side_id or binding_id, thread_id, turn_id, side=side_id is not None)
    event = CodexReplyEvents(runtime, CodexOrdinarySessions(runtime)).opened(
        key,
        origin,
        BindingTaskFeedback(progress_card_enabled=progress_enabled),
        goal=logical_turn,
        reply=reply or (lambda card: presenter._channel.reply(origin, card)),
        reply_partial=reply_partial or (lambda post: presenter._channel.reply(origin, post)),
        validate_reply=validate_reply,
    )
    return await presenter.open(event)
