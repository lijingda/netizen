"""Local-only live prompts and a conservative allowance enforced by the native CLI."""
from uuid import uuid4


class TurnBudget:
    def __init__(self):
        self.reserved = 0
        self.refused = False

    def reserve(self, limit):
        # Reserve the entire native max_turns before starting. An interrupted or
        # uncertain execution never refunds capacity based on a missing Result.
        if self.refused or limit <= 0 or self.reserved + limit > 30:
            self.refused = True
            raise RuntimeError("live model turn budget exhausted")
        self.reserved += limit
        return limit


class LiveSmoke:
    def __init__(self):
        self.budget = TurnBudget()
        self.project_token = uuid4().hex
        self.first_token = uuid4().hex
        self.handoff_token = uuid4().hex
        self.release_token = uuid4().hex

    def prompt(self, label, scenario):
        if scenario in ("foreground", "foreground-b"):
            return (f"Use Bash now to run exactly ./fixture-{scenario} in the foreground. Wait for its exit "
                    "and then briefly acknowledge completion. Do not inspect or modify the helper.")
        if scenario == "background":
            return ("Use Bash now to run exactly ./fixture-background with run_in_background set to true. "
                    "Reply immediately after launch. Do not poll the task or read its output.")
        if scenario == "denied-touch":
            return ("Use Bash once to run exactly touch permission-probe in the project directory. "
                    "If permission is denied, report the refusal briefly without retrying or using another tool.")
        prompts = {
            "ordinary": (f"Remember this conversation token {self.first_token}. Without tools, reply with the "
                         "project sentinel supplied in your project instructions."),
            "resume": "Without tools, recall the conversation token I gave you earlier.",
            "handoff": (f"Remember this separate native handoff token {self.handoff_token}. Without tools, "
                        "reply with the earlier conversation token."),
            "after-cli": "Without tools, recall the separate native handoff token.",
            "terminal-turn": f"Remember this terminal release token {self.release_token}. Reply briefly without tools.",
            "after-release": "Without tools, recall the terminal release token I gave you.",
        }
        return prompts.get(label, "Reply with a brief acknowledgement without using tools.")

    def check_answer(self, label, answer):
        assert isinstance(answer, str) and answer.strip()
        expected = {"ordinary": self.project_token, "resume": self.first_token,
                    "handoff": self.first_token, "after-cli": self.handoff_token, "after-release": self.release_token}
        if label in expected:
            assert expected[label] in answer, "native context token missing"
