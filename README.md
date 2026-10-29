# ShoggothTwenty

A Moltbook agent that runs 24/7 on free tiers: it watches specific submolts and
topics, reports what's being discussed to Telegram, and — once it's earned that
trust — participates as a defined persona.

**Status: early.** The read-only half is built and tested (`run`, `bootstrap`,
`self-check`). The participating half is designed but deliberately not written
yet: this agent starts in **L0 shadow mode**, where it proposes and a human
approves, because Moltbook's terms make the owner solely responsible for
everything the agent publishes.

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

Both workflows read secrets from an environment called `moltbook-agent`, which
has to exist before the first scheduled run:

1. **Settings → Environments → New environment** → name it `moltbook-agent`.
   Under *Deployment branches*, restrict it to the default branch only.
2. Add three secrets to that environment: `MOLTBOOK_API_KEY`,
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. **Settings → Actions → General** → under *Fork pull request workflows from
   outside collaborators*, choose **Require approval for all outside
   collaborators**. The repo is public and holds secrets, so this matters.
4. Do not attach a payment method — public repositories are free and uncapped,
   so there is nothing to overrun.

Then dispatch the `heartbeat` workflow manually with `dry_run` checked to confirm
it works before waiting for the schedule.

## Commands

| Command | What it does |
|---|---|
| `run` | One heartbeat: observe → triage → report to Telegram. Read-only |
| `bootstrap` | Mark the current feed head as seen, so a cold start doesn't flood you |
| `commit-state` | Arm a state commit if the interval elapsed (exit 3 = skip, not an error) |
| `self-check` | Validate credentials, report `claimed` vs `pending_claim` |
| `register` | Create the agent and store the one-time key. Requires `--confirm` |

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
