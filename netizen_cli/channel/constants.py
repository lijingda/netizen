"""Timeouts, limits, reaction names and fixed texts shared by Channel layers."""

from __future__ import annotations

import re




TURN_FILES_WITHOUT_FINAL_RESPONSE = "任务已完成，已生成以下文件。"
TEXTUAL_CONTENT_TYPES = frozenset({"text", "post"})
SESSION_QUERY_TIMEOUT_SECONDS = 10.0
SIDE_CARD_METADATA_TIMEOUT_SECONDS = 1.0
CONTEXT_RECEIPT_TIMEOUT_SECONDS = 5.0
SESSION_RECEIPT_TIMEOUT_SECONDS = 5.0
TERMINAL_ANSWER_DELIVERY_TIMEOUT_SECONDS = 10.0
DONE_REACTION = "DONE"
ERROR_REACTION = "ERROR"
INTERRUPTED_REACTION = "CrossMark"
STEER_REACTION = "OnIt"
# Feishu reuses 230099/11310 for locks and permanent card limits. Only the
# observed lock reason in the SDK's public SendError.hint permits a retry.
# Remove this retry after card-action dispatch is ordered behind its Feishu ack.
FEISHU_CARD_ACTION_LOCK_OUTER_CODE = 230099
FEISHU_CARD_ACTION_LOCK_INNER_CODE = 11310
CARD_ACTION_LOCK_RETRY_DELAYS_SECONDS = (0.2, 0.5)
QUESTION_SUBMISSION_CARD_TIMEOUT_SECONDS = 5.0
CARD_CHAT_QUERY_SECONDS = 8.0
CARD_CHAT_QUERY_PAGE_SIZE = 20
CARD_CHAT_QUERY_MAX_PAGES = 20
SESSION_TITLE_MAX_CHARS = 48
STATUS_THREAD_NAME_MAX_CHARS = 120
STATUS_THREAD_PREVIEW_MAX_CHARS = 240
THREAD_PREVIEW_TRAILER = re.compile(
    r'<feishu_current_message_context>\s*\{\s*"kind"\s*:\s*"feishu_current_message"'
    r'|<feishu_card_answer_context>\s*\{\s*"kind"\s*:\s*"feishu_question_answer"'
    r'|<scheduled_plan>\s*\{\s*"kind"\s*:\s*"scheduled_plan"'
)
STATUS_PLAN_MAX_STEPS = 12
SIDE_ROOT_UUID_PREFIX = "side-root-"
SIDE_SEED_UUID_PREFIX = "side-seed-"
SIDE_INITIAL_QUESTION_MAX_CHARS = 3000
SIDE_EMPTY_TOPIC_PROMPT = "在本话题发送第一条问题，开始 Side 对话。"
FEISHU_AT_TAG_START = re.compile(r"<(?=/?at(?:\s|>|/))", re.IGNORECASE)
FEISHU_CONTENT_AUDIT_REJECTION_CODE = 230028
FEISHU_AUDIT_REASON_LABELS = {
    "EMAIL_ADDRESS": "邮箱地址",
}
