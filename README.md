# ShoggothTwenty

A read-only Moltbook agent: polls the feed on a schedule, scores posts against a small set of
topics, and pushes a batched digest to Telegram. Runs entirely on GitHub Actions' free tier — no
server, no paid API.

**Current state: planning.** [`PLAN.md`](PLAN.md) is the reviewed implementation plan. No code yet.

Decisions so far: private repo, hourly polling, batched digests, deterministic rules (no LLM),
read-only against Moltbook.
