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

1. **Bucket closures by ISO week** over a trailing window, from the
   activity log — `task.status_changed` into `done`, which
   `_done_days()` already derives. The result is a short series of
   weekly throughputs, zeros included.
2. **Bucket scope arrivals the same way**, from
   `task.milestone_changed` — `_membership_history()` already gives the
   join and leave spans.
3. **Simulate the remaining work many times.** Each trial draws a random
   historical week, subtracts its throughput from the remainder, adds
   that week's scope growth, and repeats until the remainder is gone.
   The trial's length is one possible answer.
4. **Read the answers off the distribution**: the date half the trials
   beat, the date 85% of them beat, and — the number that actually
   decides anything — the share of trials that land on or before the
   target.

Throughput and scope growth are **drawn from the same historical week**,
not independently. A week in which a lot was closed is often a week in
which a lot arrived; sampling them apart would throw that correlation
away and narrow the result dishonestly.

### What the page says

The verdict sentence becomes a probability first and dates second:

> **38% chance of making Dec 1** · half the runs land by Dec 9, 85% by Dec 22

The chart's single dotted projection becomes two: the 50th and 85th
percentile. A fan, not a line.

### When we refuse to answer

Below **four weeks of history** or **five closed tasks**, there is no
forecast — the page says so and draws no projection. This replaces the
`0.05` floor. "Not enough history yet" is a true statement about a young
milestone; "865 days late" is not.

### Parameters

| Knob | Value | Why |
|---|---|---|
| Window | trailing 12 weeks | Long enough to hold a team's rhythm, short enough to forget a dead quarter |
| Trials | 10 000 | Percentiles stable to the day; a fraction of a millisecond |
| Reported | P50, P85, P(≤ target) | One honest headline plus the band behind it |
| Floor | none | Replaced by the refusal above |

### One implementation, several surfaces

The simulation lives in its own module and takes only what it needs —
closure days, the remainder, and the scope history. The milestone
burndown is its first caller; the cycle card in My Work is its second,
where `at this pace N carry over` is the same naive average under
another name. One reader per fact, as everywhere else here.

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
- Forecasts move when scope moves. Adding work late pushes the band out,
  which is correct and will occasionally be unwelcome.
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
