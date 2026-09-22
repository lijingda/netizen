"""Explicit Channel test environments with caller-owned async lifetimes."""

from __future__ import annotations

import itertools
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager, closing
from dataclasses import dataclass, field
from pathlib import Path

from lark_channel import OutboundCard

from netizen import channel_app
from netizen.bindings import BindingStore, BindingTaskFeedback
from netizen.cards import goal_generation, reply_card
from netizen.channel import reply_presenter
from netizen.channel_app import ChannelApplication
from netizen.domain import FeishuScope, NativeCapability, ReplyCardProjection, ScopeKind
from netizen.management import (
    InstanceManagementService,
    ManagementRuntimePort,
    ScopeCoordinator,
)
from netizen.projects import ProjectRegistry
from netizen.runtime.contracts import Submission, SubmitDisposition
from netizen.schedules.models import ScheduleRule
from netizen.sdk_gap_adapter import GoalSnapshot
from tests.support.channel_messages import FakeChannel, FakeMessage, FakeMessageHistory
from tests.support.channel_results import sent_result
from tests.support.channel_runtime import StubRuntime


@dataclass
class ChannelFixture:
    project_root: Path
    project: Path
    store: BindingStore
    channel: FakeChannel
    runtime: StubRuntime
    projects: ProjectRegistry
    management: InstanceManagementService
    app: ChannelApplication
    message_history: FakeMessageHistory | None

    async def new(self, *, message_id: str = "om_new") -> FakeMessage:
        message = FakeMessage("/new", message_id=message_id)
        await self.create_binding(
            FeishuScope("cli_test", "oc_direct", ScopeKind.DIRECT)
        )
        return message

    async def create_binding(self, scope: FeishuScope):
        return await self.app._management.create_current_binding(
            scope=scope,
            creator_id="ou_user",
            project_alias="test",
        )

    async def register_goal_card(
        self,
        *,
        scope: FeishuScope,
        binding,
        goal: GoalSnapshot,
        message_id: str,
        runtime_state: str,
        logical_turn_id: str = "goal-one",
    ) -> OutboundCard:
        if binding.native_thread_id is None:
            self.store.assign_native_thread_id(binding.id, goal.thread_id)
            binding = self.store.get(binding.id)
        projection = ReplyCardProjection(
            scope=scope,
            goal=channel_app._reply_goal_module(
                binding=binding,
                goal=goal,
                runtime_state=runtime_state,
            ),
        )
        generation = goal_generation(goal)
        assert (
            await self.app._progress_cards.start_goal(
                binding_id=binding.id,
                thread_id=goal.thread_id,
                logical_turn_id=logical_turn_id,
                generation=generation,
                origin=reply_presenter.GoalCardOrigin(
                    message_id=message_id,
                    scope=scope,
                    binding_id=binding.id,
                    short_id=binding.short_id,
                    project_alias=binding.project_alias,
                ),
                projection=projection,
                revision=("test",),
                refresh=None,
            )
        )
        self.channel.updates.clear()
        return reply_card(projection)


@dataclass
class SideChannelFixture(ChannelFixture):
    """An ordinary Channel environment with Side support and unlimited IDs."""

    def binding_for(
        self,
        message: FakeMessage,
        *,
        task_feedback: BindingTaskFeedback = BindingTaskFeedback(),
    ):
        scope = self.app._scope(message)
        binding = self.store.create_binding(
            scope=scope,
            project_alias="test",
            creator_id="ou_owner",
            task_feedback=task_feedback,
        )
        self.store.assign_native_thread_id(binding.id, f"native-{binding.id}")
        return self.store.get(binding.id)

    def queue_promoted_topic(
        self,
        *,
        chat_id: str,
        root_id: str,
        seed_id: str,
        topic_id: str,
    ) -> None:
        self.channel.send_results.extend(
            (
                sent_result(root_id, chat_id=chat_id),
                sent_result(
                    seed_id,
                    chat_id=chat_id,
                    thread_id=topic_id,
                    root_id=root_id,
                    parent_id=root_id,
                ),
            )
        )


@dataclass
class ScheduledChannelFixture(ChannelFixture):
    submissions: list[dict] = field(default_factory=list)
    receipts: list[str] = field(default_factory=list)

    def manager_plan(self, name, *, chat_id="oc_group", enabled=True):
        created = self.store.schedules.create(name=name, instructions="独立指令：" + name,
            project_alias="work", app_id="app", chat_id=chat_id, enabled=enabled,
            schedule=ScheduleRule("interval", "UTC", every_minutes=60, anchor=100),
            request_id="manager-" + name, now=100)
        return created.plan_id

    def enable_manual_claims(self):
        claims = []
        self.management.schedules._clock = lambda: 160.0

        def claim_manual(plan_id, expected_revision, request_id, request_payload):
            result, claim = self.store.schedules.claim_manual(plan_id, app_id="app",
                expected_revision=expected_revision, request_id=request_id, request_payload=request_payload, now=160)
            if claim is not None:
                claims.append(claim)
            return result

        self.management.schedules.set_run_now_handler(claim_manual)
        return claims

    async def submit_initial(self, **kwargs):
        self.submissions.append(kwargs)
        binding = kwargs["binding"]
        self.store.assign_native_thread_id(binding.id, "native-" + kwargs["run_id"])
        self.store.schedules.set_run(kwargs["run_id"], phase="handed_off", initial_turn_id="turn-initial")
        return Submission(
            SubmitDisposition.STARTED, binding.id, "native-" + kwargs["run_id"],
            "turn-initial", lambda: self.receipts.append(kwargs["run_id"]),
            task_feedback=binding.task_feedback,
        )


@asynccontextmanager
async def _assembled_channel(
    root: Path, project: Path, store: BindingStore, *, app_id: str,
    project_alias: str, fixture_type: type[ChannelFixture],
    history: FakeMessageHistory | None,
):
    channel = FakeChannel()
    runtime = StubRuntime()
    runtime.binding_store = store
    projects = ProjectRegistry(store=store, project_root=root, projects={project_alias: project})
    management = InstanceManagementService(
        bindings=store, projects=projects, runtime=ManagementRuntimePort(runtime),
        scope_coordinator=ScopeCoordinator(),
    )
    async with AsyncExitStack() as resources:
        resources.push_async_callback(management.close)
        app = ChannelApplication(
            app_id=app_id, channel=channel, runtime=runtime, bindings=store,
            projects=projects, management=management, message_history=history,
        )
        resources.push_async_callback(app.close)
        fixture = fixture_type(root, project, store, channel, runtime, projects, management, app, history)
        yield fixture


@asynccontextmanager
async def channel_fixture():
    """The standard direct/group Channel fixture, including message history."""
    ids = iter((
        "11111111-0000-0000-0000-000000000001",
        "22222222-0000-0000-0000-000000000002",
        "33333333-0000-0000-0000-000000000003",
    ))
    with tempfile.TemporaryDirectory() as directory, closing(BindingStore(id_factory=lambda: next(ids))) as store:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        async with _assembled_channel(
            root, project, store, app_id="cli_test", project_alias="test",
            fixture_type=ChannelFixture, history=FakeMessageHistory(),
        ) as fixture:
            yield fixture


@asynccontextmanager
async def side_channel_fixture():
    ids = itertools.count(1)
    with tempfile.TemporaryDirectory() as directory, closing(BindingStore(id_factory=lambda: f"record-{next(ids)}")) as store:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        async with _assembled_channel(
            root, project, store, app_id="cli_test", project_alias="test",
            fixture_type=SideChannelFixture, history=None,
        ) as fixture:
            fixture.runtime.available_capabilities = frozenset({NativeCapability.SIDE})
            yield fixture


@asynccontextmanager
async def scheduled_channel_fixture():
    with tempfile.TemporaryDirectory() as directory, closing(BindingStore(wall_clock=lambda: 160.0)) as store:
        root = Path(directory)
        async with _assembled_channel(
            root, root, store, app_id="app", project_alias="work",
            fixture_type=ScheduledChannelFixture, history=FakeMessageHistory(),
        ) as fixture:
            fixture.runtime.submit_initial = fixture.submit_initial
            yield fixture
