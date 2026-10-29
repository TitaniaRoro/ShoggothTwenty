#!/usr/bin/env python3
"""ShoggothTwenty — command line entrypoint.

Subcommands
-----------
run          One heartbeat: observe, triage, report to Telegram. (M1: read-only.)
bootstrap    Mark the current feed head as seen without reporting anything.
commit-state Decide whether this run should commit its state back to git.
self-check   Validate credentials and report claim status.
register     Create the agent on Moltbook and store its one-time API key.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shoggoth.actions import Actor
from shoggoth.audit import AuditLog
from shoggoth.challenge import ChallengeSolver
from shoggoth.config import ConfigError, Settings, utc_now_iso
from shoggoth.digest import render_alert, render_digest, render_proposal
from shoggoth.moltbook import MoltbookClient
from shoggoth.observe import collect
from shoggoth.persona import (
    build_model,
    comment_prompt,
    load_instructions,
    system_prompt,
    validate_draft,
)
from shoggoth.policy import AUTO_ACTIONS, Policy
from shoggoth.state import State
from shoggoth.telegram import Telegram
from shoggoth.triage import TriageConfig, is_blocked, score

log = logging.getLogger("shoggoth")

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_NOTHING_TO_COMMIT = 3

STATE_COMMIT_INTERVAL_MINUTES = 60

# How many drafts we'll put in front of a human per day. Three per run at a
# 30-minute cadence would be 144 approval requests a day.
MAX_PROPOSALS_PER_DAY = 6


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _in_quiet_hours(now: datetime, start: int, end: int) -> bool:
    """True if ``now`` falls inside a quiet window that may wrap past midnight."""
    hour = now.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _should_notify(
    state: State, config: TriageConfig, has_priority: bool
) -> tuple[bool, str]:
    """Apply the batching policy. Returns (send_now, reason)."""
    now = datetime.now(timezone.utc)

    if not state.pending_report:
        return False, "nothing buffered"

    if has_priority:
        return True, "priority item bypasses batching"

    if _in_quiet_hours(now, *config.quiet_hours_utc):
        return False, "quiet hours"

    if state.last_notify_at:
        try:
            last = datetime.strptime(state.last_notify_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            last = None
        if last and now - last < timedelta(minutes=config.min_interval_minutes):
            return False, f"within {config.min_interval_minutes} min of the last digest"

    return True, "interval elapsed"


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(require_moltbook=True, require_telegram=True)
    config = TriageConfig.load(settings.topics_path)
    state = State.load(settings.state_path)
    state.touch_run()

    if state.kill_switch:
        log.warning("kill switch is on; no actions will be proposed")

    client = MoltbookClient(settings)
    telegram = Telegram(
        settings.telegram_bot_token, settings.telegram_chat_id, dry_run=settings.dry_run
    )
    audit = AuditLog()

    # 1. Observe. A heartbeat that can't read the feed is a broken heartbeat and
    #    must not be reported as "nothing new".
    try:
        observations = collect(client, settings, config.submolts)
    except RuntimeError as exc:
        # The message contains an error string from the API, so it gets escaped
        # like any other third-party text.
        log.error("%s", exc)
        audit.record("run_failed", error=str(exc))
        telegram.send(render_alert("ShoggothTwenty heartbeat failed", str(exc)))
        return EXIT_FATAL

    audit.record("run_start", posts_seen=len(observations), submolts=len(config.submolts))
    log.info("collected %d posts across %d submolts", len(observations), len(config.submolts))

    # 2. Triage everything we haven't already processed.
    reportable: list[dict] = []
    for observation in observations:
        if state.is_seen(observation.id, observation.fingerprint):
            continue
        state.mark_seen(observation.id, observation.fingerprint)

        if is_blocked(observation, config):
            log.debug("blocked: %s", observation.id)
            continue

        value, matched = score(observation, config)
        if value < config.score_threshold:
            continue

        reportable.append(
            {
                "id": observation.id,
                "title": observation.title,
                "submolt": observation.submolt,
                "author": observation.author,
                "score": value,
                "matched": matched,
                "url": observation.url,
                "first_seen": utc_now_iso(),
            }
        )

    state.prune_seen()
    log.info("%d posts cleared the %.1f threshold", len(reportable), config.score_threshold)

    # 3. Buffer them. If we're inside a batching window, they wait for the next
    #    digest rather than being lost.
    for item in reportable:
        state.add_pending_report(item)
    dropped = state.drop_stale_report(config.max_catchup_minutes)

    # 4. Notify policy.
    has_priority = any(
        item.get("score", 0.0) >= config.priority_score for item in state.pending_report
    )
    send_now, reason = _should_notify(state, config, has_priority)
    log.info("notify decision: %s (%s)", "send" if send_now else "hold", reason)
    audit.record(
        "notify_decision",
        send=send_now,
        reason=reason,
        buffered=len(state.pending_report),
        priority=has_priority,
    )

    if send_now:
        buffered = sorted(
            state.pending_report, key=lambda item: item.get("score", 0.0), reverse=True
        )
        shown = buffered[: config.max_items_per_digest]
        suppressed = len(buffered) - len(shown) + dropped
        if telegram.send(render_digest(shown, suppressed=suppressed)):
            state.clear_pending_report()
            state.mark_notified()

    if settings.dry_run:
        log.info("dry run: state not persisted")
        return EXIT_OK

    state.save(settings.state_path)
    return EXIT_OK


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Mark the current feed head as seen, without reporting anything.

    Without this, a cold start would treat the entire visible feed as new and
    flood Telegram on the first run.
    """
    settings = Settings.from_env(require_moltbook=True, require_telegram=False)
    config = TriageConfig.load(settings.topics_path)
    state = State.load(settings.state_path)

    client = MoltbookClient(settings)
    try:
        observations = collect(client, settings, config.submolts)
    except RuntimeError as exc:
        log.error("%s", exc)
        return EXIT_FATAL

    for observation in observations:
        state.mark_seen(observation.id, observation.fingerprint)
    state.prune_seen()
    state.clear_pending_report()
    state.touch_run()

    log.info("bootstrap: marked %d posts as already seen", len(observations))
    if not settings.dry_run:
        state.save(settings.state_path)
    return EXIT_OK


def cmd_commit_state(args: argparse.Namespace) -> int:
    """Gate the state commit to at most once per interval.

    These commits carry double duty: they persist dedupe state, and they are the
    repository activity that stops GitHub disabling the schedule after 60 days.
    Removing this step would silently kill the heartbeat in two months.
    """
    settings = Settings.from_env(require_moltbook=False, require_telegram=False)
    state = State.load(settings.state_path)

    if not state.should_commit(STATE_COMMIT_INTERVAL_MINUTES):
        log.info("state commit skipped (interval not elapsed)")
        return EXIT_NOTHING_TO_COMMIT

    state.mark_committed()
    state.save(settings.state_path)
    log.info("state commit armed")
    return EXIT_OK


def _resolve_approvals(telegram: Telegram, state: State) -> list[dict]:
    """Collect button presses since the last heartbeat and apply them."""
    callbacks, offset = telegram.poll_callbacks(state.telegram_update_offset)
    state.telegram_update_offset = offset
    if not callbacks:
        return []

    resolved = []
    for callback in callbacks:
        for proposal in state.pending_approvals:
            if proposal.get("id") == callback["id"] and proposal.get("status") == "pending":
                proposal["status"] = callback["action"]
                resolved.append(proposal)
                log.info("proposal %s %sed", callback["id"], callback["action"])
    return resolved


def _execute_approved(state: State, actor: Actor, telegram: Telegram, audit: AuditLog) -> int:
    """Run the proposals a human approved. Never touches anything else."""
    executed = 0
    for proposal in list(state.pending_approvals):
        if proposal.get("status") != "approved":
            continue

        kind = proposal["kind"]
        result = (
            actor.create_post(proposal["target"], proposal["title"], proposal["text"], approved=True)
            if kind == "post"
            else actor.create_comment(proposal["target"], proposal["text"], approved=True)
        )

        if result.ok:
            executed += 1
            proposal["status"] = "done"
            log.info("executed approved %s on %s", kind, proposal["target"])
        elif result.deferred:
            # Cooldown: leave it approved and let a later heartbeat finish it.
            log.info("approved %s deferred: %s", kind, result.error)
        else:
            proposal["status"] = "failed"
            proposal["error"] = result.error
            telegram.send(
                render_alert(
                    "Approved action failed",
                    f"{kind} on {proposal['target']}: {result.error}",
                )
            )
        audit.record("approved_action", kind=kind, target=proposal["target"], ok=result.ok)

    # Keep the list bounded: finished proposals are history, not state.
    state.pending_approvals = [
        proposal for proposal in state.pending_approvals if proposal.get("status") in ("pending", "approved")
    ]
    return executed


def cmd_propose(args: argparse.Namespace) -> int:
    """L0 shadow mode: draft what the agent would say, and ask a human first.

    Three phases in one run:
      1. Collect approvals from previous runs (Telegram button presses).
      2. Execute anything already approved.
      3. Draft new candidates and send them for approval.
    """
    import uuid

    settings = Settings.from_env(require_moltbook=True, require_telegram=True)
    config = TriageConfig.load(settings.topics_path)
    state = State.load(settings.state_path)
    state.touch_run()

    audit = AuditLog()
    model = build_model()
    solver = ChallengeSolver(state, model)
    policy = Policy.for_state(state, is_new_agent=args.new_agent)
    client = MoltbookClient(settings)
    actor = Actor(client, policy, solver, audit)
    telegram = Telegram(
        settings.telegram_bot_token, settings.telegram_chat_id, dry_run=settings.dry_run
    )

    if state.kill_switch:
        log.warning("kill switch is on; drafting is disabled")
        return EXIT_OK

    log.info("model=%s level=%s | %s", model.name, state.autonomy_level, policy.summary())

    _resolve_approvals(telegram, state)
    executed = _execute_approved(state, actor, telegram, audit)
    if executed:
        log.info("executed %d approved actions", executed)

    if solver.locked_out:
        telegram.send(
            render_alert(
                "Verification lockout",
                "The agent stopped attempting challenges after repeated failures. "
                "Clear state.challenge.consecutive_failures before it writes again — "
                "ten consecutive failures suspend the account automatically.",
            )
        )
        log.error("challenge lockout; refusing to draft")
        if not settings.dry_run:
            state.save(settings.state_path)
        return EXIT_OK

    # Draft candidates from the highest-scoring recent observations.
    try:
        observations = collect(client, settings, config.submolts)
    except RuntimeError as exc:
        log.error("%s", exc)
        return EXIT_FATAL

    candidates = []
    for observation in observations:
        if observation.submolt in config.never_engage:
            continue
        if is_blocked(observation, config):
            continue
        value, matched = score(observation, config)
        if value >= config.score_threshold:
            candidates.append((value, observation))
    candidates.sort(key=lambda pair: pair[0], reverse=True)

    instructions = load_instructions(settings.instructions_path)
    system = system_prompt(instructions)

    # Cap how many drafts we put in front of a human. Three per run at a 30-minute
    # cadence would be 144 approval requests a day, which is exactly the kind of
    # noise that gets an agent ignored.
    proposals_today = state.budgets.get("proposals_today", 0)
    remaining_today = max(0, MAX_PROPOSALS_PER_DAY - proposals_today)
    if remaining_today == 0:
        log.info("daily proposal cap reached (%d); not drafting", MAX_PROPOSALS_PER_DAY)
        if not settings.dry_run:
            state.save(settings.state_path)
        return EXIT_OK

    drafted = 0
    for value, observation in candidates[: min(args.max_drafts, remaining_today)]:
        if state.already_proposed(observation.id):
            continue  # we've already put this one in front of a human
        if any(p.get("target") == observation.id for p in state.pending_approvals):
            continue  # already asked about this one

        raw = model.complete(system, comment_prompt(observation.title, observation.content, observation.submolt))
        if raw is None:
            log.warning("model unavailable; skipping %s", observation.id)
            break

        draft = validate_draft(raw, "comment")
        if not draft.ok:
            log.info("draft rejected for %s: %s", observation.id, draft.reason)
            continue

        proposal_id = uuid.uuid4().hex[:8]
        state.pending_approvals.append(
            {
                "id": proposal_id,
                "kind": "comment",
                "target": observation.id,
                "text": draft.text,
                "status": "pending",
                "score": value,
                "created_at": utc_now_iso(),
            }
        )
        telegram.send_approval(
            render_proposal(observation, draft.text, value, proposal_id), proposal_id
        )
        state.mark_proposed(observation.id)
        state.budgets["proposals_today"] = proposals_today + drafted + 1
        audit.record("draft", kind="comment", target=observation.id, score=value)
        drafted += 1

    log.info("drafted %d proposals for approval", drafted)

    if settings.dry_run:
        log.info("dry run: state not persisted")
        return EXIT_OK

    state.save(settings.state_path)
    return EXIT_OK


def cmd_control(args: argparse.Namespace) -> int:
    """Move the autonomy level or flip the kill switch.

    This is the emergency brake, so it works without any network credential.
    """
    settings = Settings.from_env(require_moltbook=False, require_telegram=False)
    state = State.load(settings.state_path)

    if args.level:
        if args.level not in AUTO_ACTIONS:
            log.error("unknown level %s (choose from %s)", args.level, ", ".join(AUTO_ACTIONS))
            return EXIT_FATAL
        state.autonomy_level = args.level
        log.info("autonomy level set to %s", args.level)

    if args.kill is not None:
        state.kill_switch = args.kill
        log.info("kill switch %s", "ON" if args.kill else "off")

    if args.clear_challenge:
        state.challenge = {}
        log.info("challenge failure streak cleared")

    state.save(settings.state_path)
    return EXIT_OK


def cmd_self_check(args: argparse.Namespace) -> int:
    """Validate every credential and report claim status. Non-zero on any failure."""
    moltbook_only = getattr(args, "moltbook_only", False)
    settings = Settings.from_env(require_moltbook=True, require_telegram=not moltbook_only)
    client = MoltbookClient(settings)

    result = client.get_agent_status()
    if not result.ok:
        log.error("Moltbook: %s %s", result.error, result.hint or "")
        return EXIT_FATAL
    status = result.data.get("status", "unknown")
    log.info("Moltbook: key is valid, status=%s", status)
    if status != "claimed":
        log.warning(
            "agent is not claimed yet — it cannot post, comment or vote until you "
            "complete the claim flow. Observation and reporting still work."
        )

    if moltbook_only:
        log.info("--moltbook-only: skipping the Telegram check")
        return EXIT_OK

    telegram = Telegram(settings.telegram_bot_token, settings.telegram_chat_id)
    try:
        import requests

        response = requests.get(
            f"{telegram._base}/getMe", timeout=settings.request_timeout
        )
        body = response.json()
    except Exception as exc:  # noqa: BLE001 - self-check should never mask the CLI
        log.error("Telegram: request failed: %s", exc)
        return EXIT_FATAL
    if not body.get("ok"):
        log.error("Telegram: %s", body.get("description"))
        return EXIT_FATAL
    log.info("Telegram: bot @%s is reachable", body["result"].get("username"))

    config = TriageConfig.load(settings.topics_path)
    log.info(
        "persona: %d submolts watched, %d topics, threshold %.1f",
        len(config.submolts),
        len(config.topics),
        config.score_threshold,
    )
    return EXIT_OK


def cmd_register(args: argparse.Namespace) -> int:
    """Register the agent and store the API key, which is shown exactly once.

    This creates a permanent public identity and consumes a name, so it demands
    an explicit --confirm.
    """
    if not args.confirm:
        log.error(
            "refusing to register without --confirm. This creates a permanent public "
            "identity and the name cannot be changed afterwards."
        )
        return EXIT_FATAL

    import json
    import os
    import stat

    import requests

    settings = Settings.from_env(require_moltbook=False, require_telegram=False)
    description = args.description or (
        "Documents the unglamorous engineering of keeping an unattended agent alive: "
        "silent failures, state, rate limits, scheduling, and the attacks agents receive."
    )

    payload = {"name": args.name, "description": description}
    try:
        response = requests.post(
            f"{settings.moltbook_base_url}/agents/register",
            json=payload,
            timeout=settings.request_timeout,
            allow_redirects=False,
        )
        body = response.json()
    except Exception as exc:  # noqa: BLE001
        log.error("registration failed: %s", exc)
        return EXIT_FATAL

    # skill.md documents the key under "agent"; the endpoint reference shows it
    # flat under "data"; and register may not use the envelope at all. Look in
    # every plausible place rather than betting on one doc. This matters more
    # than it looks: the key is issued exactly once, so guessing wrong destroys
    # an unrecoverable credential and burns the agent name with it.
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    places = (data, body)
    for candidate in (data.get("agent"), body.get("agent")):
        if isinstance(candidate, dict):
            places = places + (candidate,)

    record = next((place for place in places if place.get("api_key")), None)
    if record is None:
        if not body.get("success"):
            log.error("registration failed: %s", body.get("error") or body)
        else:
            log.error(
                "registration returned no API key.\n"
                "The complete response is printed below. Copy it before doing anything "
                "else — if a key was issued, this is the only time it will ever be "
                "shown, and a shape mismatch here would lose it permanently:\n%s",
                json.dumps(body, indent=2, sort_keys=True),
            )
        return EXIT_FATAL
    api_key = record["api_key"]

    destination = Path(
        os.environ.get(
            "MOLTBOOK_CREDENTIALS", str(Path.home() / ".config" / "moltbook" / "credentials.json")
        )
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({"api_key": api_key, "name": args.name}, indent=2), encoding="utf-8"
    )
    # Owner-only: anyone holding this key can post as the agent.
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR)

    print("\nRegistered. Back the key up now — it is shown only once.\n")
    print(f"  name:           {args.name}")
    print(f"  credentials:    {destination}")
    print(f"  claim_url:      {record.get('claim_url') or '(not returned)'}")
    print(f"  verification:   {record.get('verification_code') or '(not returned)'}")
    print(
        "\nSave all three now. The key cannot be shown again, and the claim URL "
        "and code cannot be reissued."
    )
    print("\nNext: open the claim URL, verify your email, then post the verification tweet.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="one heartbeat: observe, triage, report").set_defaults(func=cmd_run)
    sub.add_parser("bootstrap", help="mark the current feed head as seen").set_defaults(
        func=cmd_bootstrap
    )
    sub.add_parser("commit-state", help="arm a state commit if the interval elapsed").set_defaults(
        func=cmd_commit_state
    )
    propose = sub.add_parser("propose", help="L0 shadow: draft actions and ask a human first")
    propose.add_argument("--max-drafts", type=int, default=3, help="candidates per run")
    propose.add_argument(
        "--new-agent", action="store_true",
        help="apply the harsher first-24h limits (1 post / 2h, 20 comments / day)",
    )
    propose.set_defaults(func=cmd_propose)

    control = sub.add_parser("control", help="set autonomy level or flip the kill switch")
    control.add_argument("--level", choices=sorted(AUTO_ACTIONS), help="L0 shadow … L3 autonomous")
    control.add_argument("--kill", dest="kill", action="store_true", default=None,
                        help="turn the kill switch ON")
    control.add_argument("--unkill", dest="kill", action="store_false",
                        help="turn the kill switch OFF")
    control.add_argument("--clear-challenge", action="store_true",
                        help="clear the verification failure streak after a human review")
    control.set_defaults(func=cmd_control)

    check = sub.add_parser("self-check", help="validate credentials and claim status")
    check.add_argument(
        "--moltbook-only",
        action="store_true",
        help="check the Moltbook key and claim status, skipping Telegram",
    )
    check.set_defaults(func=cmd_self_check)

    register = sub.add_parser("register", help="create the agent and store its one-time key")
    register.add_argument("--name", required=True, help="public agent name")
    register.add_argument("--description", help="one or two sentences for the profile")
    register.add_argument(
        "--confirm",
        action="store_true",
        help="required: acknowledge this creates a permanent public identity",
    )
    register.set_defaults(func=cmd_register)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        return args.func(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_FATAL
    except KeyboardInterrupt:
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
