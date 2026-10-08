# 0038 — Forecasting: a date is a distribution, not a promise

**Status:** accepted · 2026-10-07

## Context

The milestone burndown ends in a sentence people read and nothing else:

> Behind · at this pace done Dec 12, 11 days after the date

It is computed in three lines (`apps/milestones/services.py`): count the
tasks closed since the milestone was first filled, divide by the days
elapsed, divide the remainder by that rate, add it to today.

That is an average over the whole life of the milestone, reported as a
single date. Four things are wrong with it, and only the last is a bug.

**It reports a point where it has a range.** Teams do not close work at a
steady rate. A real week series looks like `4, 0, 7, 3, 0, 5` — the same
mean can hide "give or take three days" and "give or take three months",
and we print both with equal confidence. A named date is read as a
commitment; we do not have the evidence for one.

**It has no memory of recency.** A milestone that lay dead through August
drags its own forecast down in December, and a fast start followed by a
month of silence still reads optimistically. There is no window.

**It ignores scope growth.** The chart draws the scope line precisely so
that work arriving late reads as work arriving late — and then the
projection extrapolates the remaining line as if the bucket were never
topped up again. The forecast is optimistic by construction.

**It cannot say "I don't know".** The rate is floored at `0.05` tasks a
day so the division always lands somewhere. A milestone with 46 tasks and
nothing closed therefore projects 920 days out and prints "done Apr 14,
865 days after the date" — a number produced by dividing by the floor,
not by anything that happened. It reads as a broken system.

## Decision

**Forecast by resampling the team's own history, and report the result as
a distribution.** This is the method Vacanti and Magennis settled on for
flow-based planning, and it needs nothing we do not already hold.

### How it works

1. **Bucket closures by day** over a trailing window, from the activity
   log — `task.status_changed` into `done`, which `_done_days()` already
   derives. The result is a series of daily throughputs, zeros included.
2. **Simulate the remaining work many times.** Each trial draws a random
   day from the window, subtracts what was closed on it, and repeats
   until the remainder is gone. The trial's length is one possible
   answer.
3. **Read the answers off the distribution**: the date half the trials
   beat, the date 85% of them beat, and — the number that actually
   decides anything — the share of trials that land on or before the
   target.

**Days, not weeks.** A weekly bucket over a sensible window gives about
a dozen samples; a daily one gives four weeks of them. Weekends need no
special handling either way: they are days that closed nothing, the
replay draws them as often as they occur, and a method that averaged
them away would be promising work on a Sunday.

### What the page says

The verdict sentence becomes a probability first and dates second:

> **38% chance of making Dec 1** · half the runs land by Dec 9, 85% by Dec 22

The chart's single dotted projection becomes two: the 50th and 85th
percentile. A fan, not a line.

### When we refuse to answer

Below **three weeks of history** or **ten closed tasks**, there is no
forecast — the page says so and draws no projection. This replaces the
`0.05` floor. "Not enough history yet" is a true statement about a young
milestone; "865 days late" is not.

### History is the span of the closes, not the age of the container

A milestone is often filed in after the fact: the work has been running
for two months and today someone draws a box around it and puts a date
on it. The row is a day old; the effort is not. Measuring history from
`created_at` — or from the day the first task was attached — makes that
milestone wait three weeks to be told something its own activity log
already knows.

So the history a milestone is judged on is the span its closes cover
inside the window, which for the case above is the full 28 days from the
first day. Same rule as the cycle card, where a three-day cycle is
likewise younger than the work in it.

This is safe because the window does the filtering that membership
otherwise would: a task closed in June and attached in October
contributes nothing, being off the back of the window. What remains is
by construction recent work, and counting it is the right answer for the
retroactive milestone and a tolerable one for a milestone that has had
unrelated finished work swept into it — a case that requires someone to
assert, by attaching it, that the work belongs.

It also keeps the refusal honest: the sentence names a count and a
window that are now the same measurement, where before it could read
"63 tasks closed in the last 1 days" — the count over four weeks, the
window over the milestone's age.

### Points when there are points, tasks when there are not

Replaying task counts assumes every task is the same work. It is the
assumption a reader notices first and believes least — the easy work
went early, and what remains is what remains because it is harder, so
"twelve closed, twelve left" is not two equal fortnights.

`Task.size` already exists. When **both** sides clear `NEED_SIZED` — the
closes inside the window and the open work alike — the replay runs on
points instead: the daily buckets hold points closed, the remainder is
points open, and the percentiles come out of the same simulation. Below
that bar it counts tasks, as before.

Both sides have to clear it because a pace in points over a remainder in
tasks is not a ratio of anything, and the half-covered case is the one
that reads as precision while being neither. Tasks with no estimate are
imputed at the median of those that have one: dropping them would
quietly shrink the pace and the remainder together, while imputing keeps
the totals whole and costs only an accuracy that was already missing.

The refusal stays in tasks. Ten points can be one task, and one task is
not a rhythm — so `NEED_CLOSES` counts closes, whatever the replay is
weighing.

### An estimate is only as good as who made it

An agent will fill an estimate in where a person would have left it
blank, which is how coverage ever reaches the bar — see the MCP tool
rule. That is worth having: a consistent guess beats treating a
thirteen as a one. But a guess that the page then quotes back as a date
is the page quoting itself.

So `Task.size_source` records where each estimate came from. The
channel answers it everywhere but MCP: a size that arrives from the web
was typed by whoever was looking at the task. Through MCP, a number the
person dictated and a number the agent invented arrive identically, so
the tool declares it with `size_from_user`, and silence is read as the
agent. The default is the weaker claim on purpose — forgetting to say
so costs an estimate some trust, where the opposite default would let a
guess pass as evidence.

A points forecast therefore prints what share of its estimates an agent
made up, next to the dates. The finer unit must not read as finer data.

### Parameters

| Knob | Value | Why |
|---|---|---|
| Window | trailing 28 days | Long enough to hold a team's rhythm, short enough to forget a dead quarter |
| Trials | 2 000, a quarter of that past 90 days out | Percentiles steady to the day where a day changes the verdict; a milestone months out is not in doubt and need not be sampled as finely |
| Horizon | 400 days | Beyond a year the number is noise, and an unbounded loop on a stalled milestone is a hung request |
| Reported | P50, P85, P(≤ target) | One honest headline plus the band behind it |
| Floor | none | Replaced by the refusal above |
| Estimate coverage | 70% of each side | Enough of a sample to take a median of; below it, count tasks |

Cost lands between 11 ms and 37 ms, the slow end being a milestone whose
work is a year out. It is arithmetic over data `burndown()` has already
fetched, so the page gains no query.

### One implementation, several surfaces

The simulation lives in its own module and takes only what it needs —
closure days, the remainder, and the scope history. The milestone
burndown is its first caller; the cycle card in My Work is its second,
where `at this pace N carry over` is the same naive average under
another name. One reader per fact, as everywhere else here.

### Scope growth is left out, for now

A forecast that replays closes and ignores arrivals is optimistic by
construction: it assumes the bucket is never topped up again. The data
to fix that is already here — `_membership_history()` holds when every
task joined and left — and the fix is to sample arrivals from the same
historical day as the closes, so that the correlation between a busy day
and a growing one survives.

It is not in the first cut because it changes the question the page has
to answer. A milestone whose scope grows faster than it burns down never
finishes, and "never" needs a reading, a horizon and a sentence that no
one has designed yet. Shipping the simulation without arrivals is a
smaller, honest step: every number it prints is true of the work that
exists today. The next cut adds arrivals and the "at this rate it does
not converge" state together.

## Alternatives considered

**Poisson–Gamma conjugate model.** Mathematically tidier: weekly
throughput as a Poisson draw, a Gamma prior over the rate, and the
posterior predictive gives the distribution in closed form with no random
numbers. Rejected because the assumption is wrong for this data —
real weekly throughput is over-dispersed and full of structural zeros
(leave, blocked weeks, a release), and Poisson understates exactly the
variance we are trying to report. The empirical bootstrap reproduces the
shape we have instead of the shape we wish we had.

**Linear regression on the remaining line.** A point estimate again, just
more expensive, and sensitive to where the line starts. It answers the
same question no better and says nothing about spread.

**Little's Law (cycle time × WIP).** Answers a different question — how
long one item takes to cross the board. For "when is this set finished",
throughput is the right lens.

**Weighted or exponential moving average.** A cheap improvement on the
present average and it does fix recency. Still a single number with no
stated uncertainty, which is the defect we set out to remove.

**Estimating by size rather than count.** `Task.size` is nullable and
mostly empty, and asking people to fill it to get a forecast is how
estimation rituals start. Counting items is what makes this method usable
without ceremony.

## Consequences

- The headline becomes a probability. People will ask what 38% means; the
  honest answer — "about two chances in five, on the evidence of the last
  twelve weeks" — is more use than a date nobody believed anyway.
- Young and idle milestones stop producing numbers. Some pages will say
  "not enough history" where they used to show a confident date. That is
  the point, and it is the part most likely to be mistaken for a
  regression.
- Adding work late pushes the band out, because the remainder grew —
  but the replay still assumes nothing more arrives. Until arrivals are
  sampled the band is the optimistic edge of the truth.
- Two dotted lines instead of one. The chart carries more ink; the fan is
  the thing that communicates uncertainty at a glance, so it earns it.
- The simulation is deterministic per render only if we seed it. We do
  seed it, from the milestone id, so a page refresh does not shuffle the
  numbers under the reader.
- No new queries and no new tables. Every input is already computed by
  `burndown()`.

## Still open

- Whether to show the band on list surfaces (the Plan tab, the milestone
  list) or keep it on the detail page, where there is room to explain it.
- Whether a workspace should be able to set its own confidence level.
  85% is the common default; some teams plan to 95%.
