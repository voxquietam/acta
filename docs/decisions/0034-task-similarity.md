# 0034 — "This already exists": semantic neighbours for tasks

**Status:** accepted · 2026-10-02

## Context

Acta's boards carry work written in Ukrainian, Russian and English at
once — routinely inside one project, because the task is filed in the
language the person thinks in and read by someone who thinks in another.
Every search we had was `ILIKE`: substring matching that cannot tell
that "аудит сегментации сети" and *Network segmentation audit* are the
same job, and will not even match "бекапи" against "бэкапы".

The cost of that shows up as duplicates. The production database carries
five tasks titled "Експорт працівників з ІАС" — those turned out to be a
recurring rule's instances, which is its own lesson, but the general
case is real: a task gets filed twice because nobody could find the
first one.

MCP makes it sharper. A client asked to "add a task about X" has no way
to check whether X is already on the board, so it files a second copy
with perfect confidence.

## Decision

Tasks carry a vector of their title and description, produced by a
multilingual embedding model, and anything that wants to ask "what
already looks like this" compares vectors instead of letters.

**The model runs elsewhere.** `ACTA_EMBEDDING_URL` points at an Ollama
host on the network; nothing is loaded into the Django process. The
alternative — `onnxruntime` plus a quantised model in the image — was
measured at 118 MB on disk and ~430 MB of RSS *per process*, and with
uvicorn and the q-cluster each holding a copy that is most of a
gigabyte for a side feature. Against an Ollama host already running for
other reasons, the cost on our side is one HTTP call.

**The model must be multilingual, and that is not negotiable.** Measured
on the production database with the harness in
`scripts/eval_embedding_model.py`, which asks in one language for tasks
titled in another:

| model | right task in the top 5 |
|---|---|
| `bge-m3` (1024 dims, on the Ollama host) | **10/10** |
| `multilingual-e5-small` (local, CPU, 118 MB) | 9/10 |
| `nomic-embed-text` (on the same host) | 3/10 |

`nomic-embed-text` is English-trained and put *Network segmentation
audit* at rank 470 of 484 for "аудит сегментации сети". An embedding
model that is merely popular is not evidence; run the harness.

**Vectors are only comparable to their own model.** Each row stores the
model that built it and is invisible to search until rebuilt, so
changing `ACTA_EMBEDDING_MODEL` degrades to "no suggestions" rather than
to wrong ones. `manage.py backfill_embeddings` rebuilds — 945 production
tasks in 13 seconds.

**No vector database.** 945 tasks is 3.9 MB of float32. The whole
workspace is one matrix in memory and a query is one dot product
(0.2 ms), cached between requests and invalidated by a count-plus-max
`updated_at` stamp. pgvector earns its place somewhere north of 10⁵
vectors; adopting it here would mean a new Postgres image for a feature
that fits in four megabytes. Same reasoning as having no Redis.

**It ranks; it never decides.** The web dialog shows at most three
candidates under the title field and will not block a create. The MCP
tool returns scores and leaves the judgement to the client — the
instruction in `acta_task_create` asks for the check, rather than the
create tool refusing on its own. A model that is right 70% of the time
must not be able to stop someone filing work.

**Recurring instances are never neighbours.** A weekly task has dozens
of identical copies by design. They are excluded from the searchable
matrix; without that, the top suggestion for every recurring task is its
own sibling at cosine 1.000.

## Consequences

- Nothing changes for a deployment that sets no URL: the hint renders
  empty, the MCP tool says so in a `note`, and no vectors are written.
  The host being down behaves the same way, deliberately — the lookup
  logs and returns nothing rather than raising into a request.
- Vectors follow the text through a `post_save` signal rather than a
  call in each writer, because tasks are saved from the web views, the
  REST API, the MCP tools, the recurring generator and the Kaneo
  import. Saves that name their `update_fields` and touch neither title
  nor description are ignored, so a kanban drag costs nothing.
  `QuerySet.update()` fires no signal at all; the backfill command is
  the backstop for whatever drifts.
- The thresholds (0.45 for the MCP tool, 0.55 for the dialog) are
  provisional. On `bge-m3` a true match has measured anywhere between
  0.55 and 0.88, and 8% of all pairs sat above 0.92 on another model —
  a fixed cutoff is a property of the model, not of the data, which is
  why the design leans on ranking and a short list instead.
- Recall against links people made themselves, on production: 70% of
  `related` pairs in the top ten (median rank 5), 52% of `blocks`, and
  27% of `parent`. That first number is what the link picker is built
  on — an empty search box now offers neighbours instead of nothing.
  The last one is why nothing suggests a parent from text: a parent is
  an umbrella ("Модуль кадри"), a subtask is specific ("Експорт у CSV"),
  and they are not supposed to read alike.
- The same ranking answers "who usually does this", by counting the
  assignees of the near neighbours. It is the strictest consumer —
  closer neighbours, and the same person on at least two of them —
  because a wrong name beside someone's work is worse than no name. It
  is only computed for an unassigned task, which is also the only case
  the dropdown shows it, so an assigned task pays nothing. The create
  dialog answers the same question off the lookup it already made for
  the similar-task list, rather than asking the host twice per keystroke.
  Labels are suggested the same way, minus the "unassigned only"
  condition — a task with labels can still be missing one — and under a
  looser rule: two neighbours carrying it, *or* one that is very close.
  Two carriers alone made the suggestion almost never fire on a board
  that labels sparsely, and a wrong label costs a glance where a wrong
  name costs more.
- An existing task's own vector is already stored, so asking for its
  neighbours costs no round trip: the task page and the link picker do
  one dot product and a lookup by id.
- numpy becomes a hard dependency. A pure-Python cosine over a thousand
  tasks costs ~0.2 s per query against numpy's ~0.2 ms, and the lookup
  runs on every pause in typing.

## Alternatives considered

- **Postgres full-text search / `pg_trgm`.** Free and already in the
  database, and genuinely better than `ILIKE` — but it is still lexical.
  It cannot cross the Ukrainian/Russian/English gap, which is the
  specific problem we have.
- **A hosted embedding API** (OpenAI, Voyage, Cohere). The whole corpus
  would cost fractions of a cent. Rejected because production holds a
  client's audit data and there is a perfectly good GPU box on the
  network already.
- **Running the model in our own containers** (`onnxruntime` +
  `multilingual-e5-small`). Measured and kept as the fallback for a
  deployment with no Ollama host: 9/10 on the same harness, 2.8 ms per
  text, at the price of ~430 MB of RSS per process.
- **Letting `acta_task_create` refuse on a near match**, with a `force`
  escape hatch. Stronger than an instruction, which a client can ignore
  — and rejected for that same reason: the tool would be deciding what
  is a duplicate on a signal that is right most of the time, not always.
