"""Construct exact Codex presentation events for synthetic Channel tests."""

from dataclasses import replace

from netizen_cli.bindings import BindingTaskFeedback
from netizen_cli.channel.codex_reply_events import CodexReplyEvents
from netizen_cli.channel.reply_events import ActivityChanged, PresentationKey
from netizen_cli.runtime.codex_ordinary import CodexOrdinarySessions
from netizen_cli.runtime.codex_replies import ordinary_conclusion
from netizen_cli.runtime.contracts import TurnOutcome


def codex_result(outcome):
    if isinstance(outcome, TurnOutcome):
        outcome = ordinary_conclusion(outcome)
    return CodexReplyEvents.result(outcome)


def partial_events(target, items, *, goal=False):
    return tuple(target.partial(item, presentation_id=target.key.turn_id if goal else None) for item in items)


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
    snapshot_reader=None,
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
    if snapshot_reader:

        def observe():
            snap = snapshot_reader()
            if snap is None:
                return None
            return ActivityChanged(
                event.target, snap, partial_events(event.target, snap.partial_answers, goal=logical_turn)
            )

        event = replace(event, observe=observe, retain_after_send=lambda: snapshot_reader() is not None)
    return await presenter.open(event)
