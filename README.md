# ShoggothTwenty

A Moltbook agent that runs 24/7 on free tiers: it watches specific submolts and
topics, reports what's being discussed to Telegram, and — once it's earned that
trust — participates as a defined persona.

**Status: the full loop is built and tested.** It observes and reports, and it
can participate — but it starts in **L0 shadow mode**, where it drafts what it
would say and a human approves from Telegram before anything is published.
Moltbook's terms make the owner solely responsible for everything the agent
publishes, so nothing posts unattended until you decide otherwise.

See [`PLAN.md`](PLAN.md) for the reasoning, the platform constraints and the
autonomy ladder.

## Layout

```
agent.py                 CLI: run | bootstrap | commit-state | self-check | register
shoggoth/
  config.py              typed settings; fails fast, never logs a secret
  moltbook.py            envelope parsing, rate budgets, 429 handling
  observe.py             feed → normalised, validated observations
  triage.py              deterministic scoring (no model involved)
  digest.py              Telegram rendering, everything escaped
  telegram.py            HTML escaping, chunking, rate limiting
  state.py               the durable store an ephemeral runner needs
persona/
  instructions.md        the agent's standing instructions
  topics.toml            submolts, topic weights, blocklist, digest policy
.github/workflows/       heartbeat (every 30 min) and watchdog (daily)
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt     # hash-pinned
pip install pytest                  # dev deps install separately, see below

cp .env.example .env                # fill in MOLTBOOK_API_KEY + Telegram values
export $(grep -v '^#' .env | xargs) # or use direnv

python agent.py self-check          # validates both credentials, reports claim status
python agent.py bootstrap           # mark the current feed head as seen
DRY_RUN=1 python agent.py run       # one heartbeat, nothing sent
```

Run the tests from the repository root:

```bash
pytest -q
```

`requirements.txt` is hash-pinned, so pip enforces it for the whole install.
That means dev dependencies can't be installed in the same command — install
them separately, as above.

## First-time GitHub setup

Do these in order. Steps 1 and 2 are required; 3 and 4 are hardening you should
do but can defer.

### 1. Add the three secrets

**Settings → Secrets and variables → Actions → New repository secret.** Add each:

| Secret name | Where you get the value |
|---|---|
| `MOLTBOOK_API_KEY` | Printed once at registration (`agent.py register`), and stored at `~/.config/moltbook/credentials.json`. It is shown **exactly once** — if you lose it, the agent has to be re-registered |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather), after `/newbot` |
| `TELEGRAM_CHAT_ID` | Message [@userinfobot](https://t.me/userinfobot); it replies with your id. A private chat is positive; a group or channel id is negative |

Nothing else needs configuring, and none of these are ever written to the
repository.

### 2. Confirm Actions is enabled

**Settings → Actions → General** → *Actions permissions* should be **Allow all
actions and reusable workflows**. Scheduled workflows need this.

### 3. Restrict fork pull requests (recommended)

The repository is public and holds secrets, so:

**Settings → Actions → General** → under *Fork pull request workflows from
outside collaborators*, pick **Require approval for all outside collaborators**.

That plus the absence of any `pull_request_target` trigger is what keeps a fork
PR from reaching the secrets.

### 4. Optional: move the secrets into an environment

An environment lets you restrict the secrets to the default branch only. Create
one at **Settings → Environments → New environment**, add the same three secrets
to it, then uncomment the `environment:` line in both workflow files. This is
skipped by default because referencing an environment that doesn't exist will
fail the run, and it's easy to get wrong on the first attempt.

### 5. Test it before you wait for the schedule

**Actions → heartbeat → Run workflow** → leave `dry_run` **checked** → *Run
workflow*. It should complete green and print a digest in the log without
sending anything to Telegram. Then run it again unchecked to get a real message.

Unchecking `dry_run` is the only difference between "prints it" and "sends it".

## Commands

| Command | What it does |
|---|---|
| `run` | One heartbeat: observe → triage → report to Telegram. Read-only |
| `propose` | **Shadow mode.** Draft what the agent would say, send it to Telegram with Approve/Reject buttons, and execute anything already approved |
| `control` | Set the autonomy level, flip the kill switch, clear a challenge streak |
| `bootstrap` | Mark the current feed head as seen, so a cold start doesn't flood you |
| `commit-state` | Arm a state commit if the interval elapsed (exit 3 = skip, not an error) |
| `self-check` | Validate credentials, report `claimed` vs `pending_claim` |
| `register` | Create the agent and store the one-time key. Requires `--confirm` |

## Making it participate

The write path exists, but the guardrails around it are the point:

```bash
python agent.py control --level L0     # start here: draft everything, post nothing
python agent.py propose                # drafts land in Telegram with Approve / Reject
```

| Level | Unattended | Needs your tap |
|---|---|---|
| **L0** | Nothing | Everything |
| **L1** | Upvotes, comments | Posts |
| **L2** | Comments and posts, within budgets | Anything outside its declared topics |
| **L3** | Everything within budgets | Nothing — you get a daily digest and review after the fact |

```bash
python agent.py control --kill         # emergency brake, works with no credentials
python agent.py control --unkill
```

Approvals are collected by polling Telegram's `getUpdates` at the start of each
heartbeat, so there's no webhook, no public endpoint and no long-lived process.

Two things the write path refuses to do, on purpose:

- **Guess at a verification challenge.** Ten consecutive failures suspend the
  account automatically, so the solver abstains rather than answering when it
  can't parse the puzzle, and locks itself out entirely after **three**.
- **Create submolts.** Some of those challenges allow only 30 seconds, which is
  not a window an unattended batch job should bet on.

## Non-negotiables

A few things in here look like over-engineering until you know why they exist:

- **Posts are parsed from `data`, not the top level.** Reading `.get("posts")` on
  the response returns `[]` on every run, and the failure is completely silent.
- **Errors exit non-zero.** A run that can't reach the API is a red run, never
  "no new posts".
- **Everything untrusted is HTML-escaped.** Post titles are attacker-controlled;
  ~2.6% of Moltbook posts contain injection attempts.
- **IDs are validated before they go in a URL.** `^[A-Za-z0-9_-]{8,64}$`, and
  nothing else is ever interpolated into a link.
- **The state commit step is load-bearing.** It persists dedupe state *and* it's
  the repository activity that stops GitHub disabling the schedule after 60 days.
- **No links are ever fetched from post content.** The agent talks only to
  `www.moltbook.com` and `api.telegram.org`.

## License

GPL-3.0, matching the repository. See [LICENSE](LICENSE).
