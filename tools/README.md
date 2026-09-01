# Weekly automation

Generates a Quarto page for each new episode of *The Corridor of Uncertainty*
and pushes it to `main`, every Tuesday, from a remote server.

For the live site URL, page structure, and end-to-end deployment workflow, see
the [repository README](../README.md).

```
tools/
  cou_youtube.py        discovery (RSS + Data API), title parsing, repo state
  episode_generator.py  transcript -> Anthropic API -> validated .qmd
  weekly_update.py      the orchestrator that cron actually runs
  notify.py             email + WhatsApp run reports
  audit_youtube_episodes.py   ad-hoc channel/site reconciliation (unchanged behaviour)
  simulate_plan.py      offline planner dry-run from a CSV (no network, no cost)
  test_cou_youtube.py   pytest suite over the real title corpus
  run_weekly.sh         cron/systemd wrapper
  cou.env.example       configuration template
  crontab.example       crontab alternative
  systemd/              recommended scheduling units
```

---

## What it does each run

1. **Discover.** Fetches the channel's Atom feed (unauthenticated, last ~15
   uploads) *and* the YouTube Data API v3 uploads playlist (full history), then
   merges on video ID. Disagreements are reported, not silently resolved.
2. **Plan.** Joins channel videos against the site on front-matter
   `youtube_id` — not filename, not title. This is what makes re-runs
   idempotent and makes a failed week self-heal.
3. **Generate.** For each missing episode: yt-dlp metadata, full transcript,
   one Anthropic Messages API call, then validation.
4. **Gate.** `quarto render` on the new page(s). A page that will not build
   never reaches the repository — on failure the generated files are deleted
   and the run aborts before committing.
5. **Publish.** Commit and push to `main`; the existing `publish.yml` workflow
   renders to `gh-pages`.
6. **Report.** Email and/or WhatsApp summary (see [`notify.py`](notify.py)).

### Validation

Fully autonomous LLM generation needs a gate. `validate_qmd` blocks a write on:

- missing or malformed YAML front matter, or a `youtube_id` that does not match
  the video the page was generated from;
- missing required sections;
- unfilled `[BRACKETED]` template placeholders (markdown links, `[MM:SS]`
  timestamps and the deliberate `[NEEDS INPUT]` marker are excluded);
- unbalanced code fences or Quarto `:::` divs.

and warns (without blocking) on:

- any `[NEEDS INPUT]` marker left for you;
- **any block quote that does not appear verbatim in the transcript** —
  fabricated quotes are the failure mode that matters most for a public site,
  so every `> "…"` line over 25 characters is checked against the transcript
  after case-folding and punctuation-stripping.

As a calibration check, 22 of the 23 existing hand-written episode pages pass
this validator unchanged. The one that fails — `episodes/episode-001.qmd` — has
a genuine orphan `:::` at line 99, an unfilled `[co-author]` placeholder and a
truncated White Rose URL. Worth fixing by hand.

---

## Setup on the remote desktop

### 1. Clone and install

```bash
git clone git@github.com:areeslindley/corridor-of-uncertainty.git
cd corridor-of-uncertainty
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements.txt
.venv/bin/python -m pytest tools/ -q          # 44 tests, should be green
```

Quarto must be on `PATH` for the render gate. Without it the gate is skipped
with a warning rather than failing the run.

### 2. Deploy key

cron has no SSH agent, so the push needs a passphrase-less key that is *not*
your personal one:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/cou_deploy -N "" -C "cou-weekly@$(hostname)"
cat ~/.ssh/cou_deploy.pub
```

Add the public key at **GitHub → repo → Settings → Deploy keys → Add**, with
*Allow write access* ticked. A repo-scoped deploy key is preferable to an
account-wide key here: if the desktop is compromised, the blast radius is this
one repository.

`run_weekly.sh` points `GIT_SSH_COMMAND` at that key explicitly.

### 3. Configuration

```bash
mkdir -p ~/.config/corridor-of-uncertainty
cp tools/cou.env.example ~/.config/corridor-of-uncertainty/cou.env
chmod 600 ~/.config/corridor-of-uncertainty/cou.env
$EDITOR ~/.config/corridor-of-uncertainty/cou.env
```

Three secrets go in there:

| Variable | Where from |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `YOUTUBE_API_KEY` | Google Cloud console → enable *YouTube Data API v3* → Credentials → API key (restrict it to that one API) |
| `COU_SMTP_PASSWORD` | Google account → Security → **App passwords**. Requires 2-Step Verification; the normal account password will be rejected |

Optional WhatsApp ping when episodes are pushed (CallMeBot — same as morning-briefing):

| Variable | Where from |
|---|---|
| `COU_WHATSAPP_PHONE` | Your number in E.164 form, e.g. `+447700900123` |
| `COU_WHATSAPP_APIKEY` | [callmebot.com](https://www.callmebot.com/) registration |

Check notifications work before scheduling anything:

```bash
set -a; source ~/.config/corridor-of-uncertainty/cou.env; set +a
.venv/bin/python tools/notify.py --check
.venv/bin/python tools/notify.py --whatsapp-only   # WhatsApp test only
.venv/bin/python tools/notify.py                   # test all configured channels
```

### 4. First run — the backlog

The site currently stops at episode 23 (CoU 23, 16 June 2026). Catch up
deliberately, watching the output, before letting cron take over:

```bash
tools/run_weekly.sh --backfill --dry-run          # what would it do?
tools/run_weekly.sh --backfill --limit 1 --no-push  # generate one, inspect it
git diff --stat && quarto preview
tools/run_weekly.sh --backfill                     # the rest
```

`--limit` exists precisely for this: it caps API spend and lets you read one
generated page carefully before trusting the batch.

### 5. Schedule

**systemd (recommended):**

```bash
mkdir -p ~/.config/systemd/user
cp tools/systemd/cou-weekly.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cou-weekly.timer
systemctl --user list-timers cou-weekly        # confirm next fire time
loginctl enable-linger "$USER"                 # run without an active login
```

`loginctl enable-linger` is load-bearing: without it, user units only run while
you have an active session, so a headless remote desktop would silently never
fire the timer.

**crontab (if the box has no systemd):** see `tools/crontab.example`.

Test the whole path without waiting a week:

```bash
systemctl --user start cou-weekly.service
journalctl --user -u cou-weekly -f
```

---

## Design notes

**Why Tuesday *evening*, not morning.** Episodes publish on Tuesdays, and
YouTube's automatic captions typically lag publication by 30–90 minutes. A
Tuesday-morning run would miss the same day's episode every single week and
only pick it up seven days later. The timer is set to 21:00 Europe/London.

**Why a 10-day window, not 7.** A window equal to the period means a single
failed or skipped run leaves a permanent hole. Ten days gives one week of
overlap, so a missed Tuesday is picked up automatically the following Tuesday.
`Persistent=true` on the systemd timer covers the case where the desktop was
asleep at the scheduled moment.

**Why the episode number comes from the title.** `episode-NNN.qmd` is derived
from the `N` in `CoU N` / `Corridor of Uncertainty N` / `Episode N:`, never from
"highest existing + 1". Incrementing would be non-idempotent: any re-run or
out-of-order upload would permanently desynchronise the site from YouTube
numbering. Deriving it means the job can also detect gaps and refuses to
overwrite an existing page when a number collides.

**Unnumbered videos are never guessed at.** Three channel videos have no
parseable number — the trailer, the Winter Olympics one-off, and the SPOTY
bonus. These are reported by email and left for you; inventing a number for
them would corrupt the sequence.

**Transcript truncation.** The old scripts cut the transcript at 12,000
characters, roughly 2,000 words — under half a typical 30-minute episode.
Everything after that point was invisible to the model. The limit is now
120,000 characters (`COU_TRANSCRIPT_CHARS`), which fits a full episode.

**Cost.** One Opus call per episode: roughly 10–15k input tokens and ~2k output.
At one episode a week that is small change; set `COU_MODEL=claude-sonnet-5` for
roughly a fifth of it if you would rather not think about it at all.

---

## Known repository issues surfaced during this work

These are pre-existing and were **not** changed automatically:

- `episodes/old-episode-001.qmd`, `-002`, `-003` duplicate the `youtube_id` of
  the live pages. They are excluded from the `episodes/index.qmd` listing
  (which globs `episode-*.qmd`) but still render as orphan pages. Probably
  safe to delete.
- `episodes/bonus-001.qmd` and `episodes/episode-bonus-001.qmd` are the same
  video (`n4g1kiPJw3k`) with different titles and dates — the former claims
  "Episode 11", which is already episode 11. One should go.
- `episodes/episode-001.qmd` has an unbalanced `:::` div, an unfilled
  `[co-author]` and a truncated URL (see Validation above).
- `upcoming.qmd` still advertises "Episode 24" as coming soon on 9 June 2026.
  The job warns by email when a published episode number catches up with the
  one advertised there, but does not edit the page.
