"""The core orchestration note and Codex developer instructions must not drift apart."""
from __future__ import annotations

import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parent.parent / "defaults"
CORE = DEFAULTS / "standards" / "orchestration.md"
CODEX = DEFAULTS / "config" / "codex" / "developer-instructions.md"

REQUIRED = [
    "owns triage, planning, coordination, verification",
    "default to delegating",
    "strong default",
    "not a rigid rule",
    "worker-count quota",
    "without asking again",
    "ask before destructive, irreversible or out-of-scope",
    "ask one clarifying question",
    "stay within the approved scope",
    "worker-low",
    "worker-medium",
    "never general or default agents",
    "never the orchestrator's own model",
    "short reports",
    "verif",
    "creates the branch",
    "commit wip",
    "do not resume",
    "stop a subagent",
    "never pipe test output to tail",
]


class OrchestrationRules(unittest.TestCase):
    def test_core_and_codex_instructions_share_every_rule(self):
        for path in (CORE, CODEX):
            text = path.read_text(encoding="utf-8").lower()
            for phrase in REQUIRED:
                self.assertIn(phrase, text, f"{path.name} lacks rule: {phrase}")

    def test_codex_weighs_benefit_not_independence_alone(self):
        text = CODEX.read_text(encoding="utf-8").lower()
        self.assertIn("independence alone does not justify delegation", text)
        self.assertIn("meaningfully outweighs briefing, coordination, and worker overhead", text)

    def test_core_note_is_core_and_short(self):
        text = CORE.read_text(encoding="utf-8")
        self.assertIn("\ncore: true\n", text)
        body = text.split("---", 2)[2]
        self.assertLessEqual(len([l for l in body.splitlines() if l.strip()]), 18)
        self.assertLessEqual(len(body.split()), 290)

    def test_details_do_not_exempt_single_agent_from_approval(self):
        text = (DEFAULTS / "standards" / "orchestration-details.md").read_text(encoding="utf-8").lower()
        for phrase in ("may launch after stating scope", "without approval", "wait for explicit user approval"):
            self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
