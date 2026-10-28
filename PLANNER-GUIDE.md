# Sage: planner, reminders, schedules and memory

What was added to the Sage server, and how to use it.

## Event bus
Everything that happens (a timer rings, a task finishes, a plan is ready, Sage remembers something, a schedule fails) is
published once to a small event bus and stored in the database. Listeners are told immediately:
- the web page (live toasts, and a red banner with a beep when a timer or reminder rings),
- the wall display (full-screen alert, instant refresh),
- anything else via `GET /api/events?since=ID&wait=25` (long-poll) or `GET /api/events/stream` (server-sent events).

## Reminders, timers and alarms
Just say it in chat (English or Dutch). This is handled without the model, so it works the same with any model:

| You say | What happens |
| --- | --- |
| `set a timer for 10 minutes for the pasta` | countdown timer |
| `remind me to call mum tomorrow at 18:00` | one-off reminder |
| `remind me every monday at 9 to review the plan` | repeating reminder |
| `herinner me over een half uur aan de was` | Dutch works too |
| `wake me up at 7:30` | alarm |
| `what timers do I have?` / `cancel the pasta timer` | list / cancel |
| `snooze 5 minutes` / `done` | when something is ringing |

Ringing items stay on the wall display and the web page until you tap Done or Snooze.

## Work plan: what Sage does next, and in what order
- Every task has a position in one global order. Sage works on the first ready task that is assigned to it, whenever it is idle.
- **Change the order** on the Planner page (drag, or the arrows, "First", "Do now"), or in chat: `do the export plugin first`.
- **Who does it**: each task is `sage` (done in the background) or `user` (waiting for you). Physical work such as soldering, printing
  or buying is assigned to you automatically.
- **Autopilot**: pause or resume with `pause autopilot`, or limit it to a window: `only work between 22 and 7`.
- **Plans**: `make a plan for building the export plugin` drafts milestones, ordered tasks, estimates, dependencies and acceptance
  criteria in the background, then notifies you.
- **Carrying tasks out**: Sage uses earlier results, your workshop notes and memories as context, checks its work against the
  acceptance criteria (one revision pass), saves the deliverable as Markdown, and may add follow-up tasks. A task that needs you
  is marked blocked with the reason; finishing every task completes the project.
- `what are you working on?` shows the queue with time estimates.

## Tasks that need you
When Sage cannot continue (it needs a decision, a missing detail, or a physical step), the task appears under **Needs you** on the
Planner page and on the wall display, with the question Sage asked.
- **Web:** click the task (or Open). You see what it needs, the acceptance criteria, Sage's work so far and your earlier answers.
  Type your answer and press **Send to Sage and continue**: the task goes first in the queue and Sage picks it up as soon as you stop chatting,
  using your answer. For physical steps use **I did it: mark done** (add notes if you like), or **Let Sage do it instead**.
- **Wall display:** tap the row in the *Needs you* card, then **Reply to Sage** and type on the on-screen keyboard (or **I did it**).
- **Chat:** `what do you need from me?` lists everything waiting for you.
- API: `GET /api/plan/task/<id>` for the details, `POST /api/plan/task/<id>/input` with `{"text": "...", "mark_done": false}`.

## Schedules
Built in (change or switch off on the Planner page): morning brief (weekdays 07:30), weekly review (Sunday 18:00),
nightly database backup (03:00, keeps 14), memory upkeep (03:30), daily digest (23:30).
Add your own: `every monday at 9 summarise the ROV project` becomes a schedule, or use the Planner page.
If Sage was off when a schedule was due, it runs late ones (within a few hours) and skips old ones.

## Memory
- Saved immediately when you state something lasting: `remember that my printer is a Prusa MK4`, `my dog is called Max`,
  `I study chemical engineering`. Questions and anything that looks like a secret are never saved.
- `forget that ...` archives a memory (restore it on the Planner page). Pin important ones so they are always remembered.
- Recall blends keywords, optional semantic similarity (when the Ollama embedding model is installed), importance, recency and use.
- Sage's own guesses go to **Suggested** for you to accept or dismiss. Nightly upkeep merges duplicates and retires stale notes.

## API (all JSON)
`/api/events`, `/api/events/stream`, `/api/reminders` (+ `/<id>/done|cancel|snooze`), `/api/schedules` (+ `/<id>`, `/run`, `/delete`),
`/api/plan`, `/api/plan/reorder|move|run-now|autopilot|generate`, `/api/plan/task/<id>`, `/api/memories` (+ `/<id>`), `/api/wall`.

## Upgrading
Sage adds the new database columns and tables automatically at startup. Copy `data/sage.db` somewhere safe first if you want a restore point.
