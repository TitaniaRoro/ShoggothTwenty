"""The write path.

Every write goes through the same gate:

    policy → request → verification challenge → verify → record

The verification step is the dangerous one. Posts, comments and new submolts can
come back held behind an arithmetic puzzle with a five-minute deadline, and **ten
consecutive failures suspend the account automatically**. So the solver abstains
rather than guessing, and the streak is tracked in state with a hard stop at
three — we never get near ten.

Submolt creation is deliberately absent: some of those challenges allow only
**30 seconds**, which is not a window an unattended batch job should bet on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .moltbook import MoltbookClient
from .policy import Policy

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    ok: bool
    kind: str
    error: str | None = None
    deferred: bool = False          # blocked by a cooldown: try a later heartbeat
    needs_approval: bool = False    # queued for a human
    remote_id: str | None = None
    verified: bool = False


class Actor:
    """Executes writes, subject to policy and the challenge solver."""

    def __init__(self, client: MoltbookClient, policy: Policy, solver, audit=None) -> None:
        self.client = client
        self.policy = policy
        self.solver = solver
        self.audit = audit

    # -- public actions ---------------------------------------------------

    def create_post(self, submolt: str, title: str, body: str, *, approved: bool = False) -> ActionResult:
        payload = {"submolt_name": submolt, "title": title, "content": body}
        return self._write("post", "/posts", payload, target=submolt, approved=approved)

    def create_comment(self, post_id: str, body: str, *, approved: bool = False) -> ActionResult:
        payload = {"content": body}
        return self._write("comment", f"/posts/{post_id}/comments", payload, target=post_id, approved=approved)

    def upvote(self, post_id: str, *, approved: bool = False) -> ActionResult:
        return self._write("upvote", f"/posts/{post_id}/upvote", {}, target=post_id, approved=approved)

    # -- internals --------------------------------------------------------

    def _write(
        self,
        kind: str,
        path: str,
        payload: dict[str, Any],
        *,
        target: str,
        approved: bool,
    ) -> ActionResult:
        if self.policy.state.kill_switch:
            return ActionResult(False, kind, error="kill switch is on", needs_approval=False)

        # 1. Autonomy level: does a human need to sign off on this first?
        if self.policy.needs_approval(kind) and not approved:
            return ActionResult(False, kind, error="requires human approval", needs_approval=True)

        # 2. Budgets and cooldowns, checked before the request rather than after a 429.
        allowed, reason = self.policy.allows(kind)
        if not allowed:
            return ActionResult(False, kind, error=reason, deferred=True)

        # 3. Send it.
        result = self.client.request("POST", path, json=payload)
        if result is None or not result.ok:
            error = getattr(result, "error", "request failed") if result else "no response"
            if getattr(result, "deferred", False):
                return ActionResult(False, kind, error=error, deferred=True)
            return ActionResult(False, kind, error=error)

        data = result.data or {}
        verification = data.get("verification")

        # 4. Held for a challenge.
        if verification:
            ok, error = self._handle_verification(kind, verification)
            if not ok:
                return ActionResult(False, kind, error=error)

        self.policy.record(kind)
        if self.audit:
            self.audit.record("action", kind=kind, target=target, verified=bool(verification))
        log.info("%s succeeded (target=%s)", kind, target)
        return ActionResult(True, kind, remote_id=data.get("id"), verified=bool(verification))

    def _handle_verification(self, kind: str, verification: dict[str, Any]) -> tuple[bool, str | None]:
        """Solve and submit the puzzle, or refuse. Never guess."""
        code = verification.get("verification_code")
        text = verification.get("challenge_text") or ""
        expires_at = verification.get("expires_at")

        if not code:
            return False, "challenge returned without a verification code"

        # Answering an expired challenge is a guaranteed failure against the streak.
        if expires_at:
            try:
                deadline = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining < 15:
                    reason = f"only {remaining:.0f}s left on the challenge"
                    self.solver.record_failure(reason)
                    return False, reason
            except ValueError:
                pass  # unparseable expiry: carry on and try

        solved = self.solver.solve(text)
        if not solved.solved:
            failures = self.solver.record_failure(solved.reason or "no answer")
            return False, (
                f"could not solve the challenge ({solved.reason}); "
                f"failure {failures} of {self.solver.max_consecutive_failures} before lockout"
            )

        answer = solved.answer
        if answer is None:
            failures = self.solver.record_failure("solver produced no answer")
            return False, f"no answer; failure {failures}"

        log.info("submitting challenge answer=%s (method=%s)", answer, solved.method)
        verify = self.client.request(
            "POST", "/verify", json={"verification_code": code, "answer": answer}
        )

        if not verify.ok:
            failures = self.solver.record_failure(verify.error or "verification rejected")
            return False, (
                f"verification rejected: {verify.error}; "
                f"failure {failures} of {self.solver.max_consecutive_failures}"
            )

        self.solver.record_success()
        return True, None

    def note_challenge_outcome(self, result: ActionResult) -> None:
        """Hook for callers that want to alert on a lockout."""
        if not result.ok and "lockout" in (result.error or ""):
            log.error("challenge lockout reached; a human must clear state.challenge")
