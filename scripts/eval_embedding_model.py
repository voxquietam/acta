#!/usr/bin/env python3
"""Score an embedding model on the question Acta would actually ask it.

Acta's tasks are written in Ukrainian, Russian and English, often all
three inside one project, and people search in whichever language they
think in. So the test is not a leaderboard: it asks in one language for
a task titled in another and reports where that task lands among every
live task in the database.

Run it against an Ollama host after a new model is pulled::

    python3 scripts/eval_embedding_model.py --model bge-m3

Measured so far (484 live tasks, dev database, 2026-10-02):

    multilingual-e5-small   local, 118 MB int8 ONNX, CPU   9/10
    nomic-embed-text        Ollama                          3/10

Standard library only, so it runs from the host with no virtualenv: the
tasks come out of the web container through ``docker compose exec``.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request

#: Asked in one language; the task that should come back is titled in
#: another. Anything missing from the database is reported and skipped,
#: so the set can be run against a database that holds other tasks.
PROBES = [
    ("проверить доступы к файловому хранилищу", "Перевірити доступи до файлового сховища"),
    ("review the database logs", "Ревʼю логів бази даних"),
    ("обновить политику почтового шлюза", "Оновити політику поштового шлюзу"),
    ("аудит сегментации сети", "Network segmentation audit"),
    ("страница проекта грузится медленно", "Investigate slow project page"),
    ("добавить уведомления в слак", "Add Slack notifications"),
    ("шаблон постмортема", "Postmortem template"),
    ("inventory of the VPN", "Інвентаризація VPN"),
    ("почистити сайт від файлів без власника", 'Очистити сайт від "файлів-сиріт"'),
    ("backup channel review", "резервного каналу"),
]

#: Every family wants its own prefixes and gets them wrong silently when
#: they are missing — e5 drops several points without ``query:`` /
#: ``passage:``, nomic without ``search_query:`` / ``search_document:``.
PREFIXES = {
    "nomic": ("search_document: ", "search_query: "),
    "e5": ("passage: ", "query: "),
    "bge": ("", ""),
    "embeddinggemma": ("title: none | text: ", "task: search result | query: "),
    "qwen3": ("", "Instruct: Find the task that matches the request\nQuery: "),
}

#: Tasks plus, when asked for, the links people have actually made
#: between them — the only ground truth a database carries on its own.
TASK_DUMP = (
    "import json;"
    "from apps.tasks.models import Task;"
    "rows=list(Task.objects.filter(archived_at__isnull=True).values('id','title','description'));"
    "links=[(a,b,'blocks') for a,b in Task.blocks.through.objects.values_list('from_task_id','to_task_id')];"
    "links+=[(a,b,'related') for a,b in Task.related.through.objects.values_list('from_task_id','to_task_id')];"
    "links+=[(a,b,'parent') for a,b in "
    "Task.objects.filter(parent__isnull=False).values_list('id','parent_id')];"
    "print('JSON:' + json.dumps({'tasks': rows, 'links': links}, ensure_ascii=False))"
)


def prefixes_for(model: str) -> tuple[str, str]:
    """Return the (document, query) prefixes this model family expects."""
    for family, pair in PREFIXES.items():
        if family in model.lower():
            return pair
    return "", ""


def load_tasks(source: str | None) -> dict:
    """Read live tasks + their links from a JSON file or the web container."""
    if source:
        with open(source, encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {"tasks": loaded, "links": []}
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "web", "python", "manage.py", "shell", "-c", TASK_DUMP],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("JSON:"):
            return json.loads(line[5:])
    raise SystemExit("could not read tasks from the web container — is the stack up?")


def rank_of(query: list[float], vectors: list[list[float]], wanted: set[int]) -> tuple[int, int]:
    """Return (rank of the best wanted vector, index of the top one)."""
    scored = sorted(
        ((sum(a * b for a, b in zip(query, vector)), i) for i, vector in enumerate(vectors)),
        reverse=True,
    )
    rank = next((n + 1 for n, (_, i) in enumerate(scored) if i in wanted), len(scored))
    return rank, scored[0][1]


def score_against_links(tasks: list[dict], links: list, vectors: list[list[float]]) -> None:
    """Measure the model against the links people made themselves.

    For every linked pair, ask the board for the neighbours of one end and
    see whether the other end comes back in the top ten. Unlike the
    hand-written probes this needs no editing per database — but it only
    means something where the links were made by people rather than
    seeded, and it under-reports by design: a blocker is often worded
    nothing like the work it blocks.
    """
    index = {task["id"]: i for i, task in enumerate(tasks)}
    by_kind: dict[str, list[int]] = {}
    for left, right, kind in links:
        if left not in index or right not in index or left == right:
            continue
        rank, _ = rank_of(vectors[index[left]], vectors, {index[right]})
        by_kind.setdefault(kind, []).append(rank)

    if not by_kind:
        print("no links in this database to score against")
        return
    print(f"\n{'link kind':<10} {'pairs':>6} {'in top 10':>10} {'in top 25':>10} {'median rank':>12}")
    print("-" * 52)
    for kind, ranks in sorted(by_kind.items()):
        ordered = sorted(ranks)
        top10 = sum(1 for r in ranks if r <= 10) / len(ranks)
        top25 = sum(1 for r in ranks if r <= 25) / len(ranks)
        median = ordered[len(ordered) // 2]
        print(f"{kind:<10} {len(ranks):>6} {top10:>9.0%} {top25:>9.0%} {median:>12}")


def show_closest_pairs(tasks: list[dict], vectors: list[list[float]], count: int) -> None:
    """Print the most similar pairs — the duplicate candidates."""
    best: list[tuple[float, int, int]] = []
    for i, left in enumerate(vectors):
        for j in range(i + 1, len(vectors)):
            score = sum(a * b for a, b in zip(left, vectors[j]))
            if len(best) < count:
                best.append((score, i, j))
                best.sort(reverse=True)
            elif score > best[-1][0]:
                best[-1] = (score, i, j)
                best.sort(reverse=True)
    print(f"\n{count} closest pairs in the database — the duplicate candidates:")
    for score, i, j in best:
        print(f"  {score:.3f}  {tasks[i]['title'][:48]!r}  <->  {tasks[j]['title'][:48]!r}")


def embed(host: str, model: str, texts: list[str], batch: int = 32) -> list[list[float]]:
    """Embed every text through Ollama, L2-normalised so a dot is a cosine."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        end = start + batch
        payload = json.dumps({"model": model, "input": texts[start:end]}).encode()
        request = urllib.request.Request(
            f"{host}/api/embed", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise SystemExit(f"Ollama said {exc.code}: {detail}\nIs '{model}' pulled on that host?")
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach {host}: {exc.reason}")
        for vector in body["embeddings"]:
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            vectors.append([x / norm for x in vector])
    return vectors


def main() -> int:
    """Embed the database, run the probes, print the scoreboard."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="bge-m3", help="Ollama model tag, e.g. bge-m3")
    parser.add_argument("--host", default="http://192.168.97.142:11434", help="Ollama base URL")
    parser.add_argument("--tasks", help="JSON file of tasks instead of reading the container")
    parser.add_argument(
        "--links",
        action="store_true",
        help="also score against the links people made in this database (no editing needed)",
    )
    parser.add_argument("--pairs", type=int, default=0, help="print the N closest pairs (duplicate candidates)")
    args = parser.parse_args()

    doc_prefix, query_prefix = prefixes_for(args.model)
    data = load_tasks(args.tasks)
    tasks, links = data["tasks"], data.get("links") or []
    titles = [task["title"] for task in tasks]
    print(f"model: {args.model}   host: {args.host}   tasks: {len(tasks)}")

    started = time.perf_counter()
    vectors = embed(
        args.host,
        args.model,
        [f"{doc_prefix}{t['title']}\n{(t['description'] or '')[:800]}" for t in tasks],
    )
    spent = time.perf_counter() - started
    print(
        f"embedded in {spent:.1f}s ({spent / len(tasks) * 1000:.0f} ms/task), "
        f"{len(vectors[0])} dimensions, {len(vectors) * len(vectors[0]) * 4 / 1e6:.1f} MB stored"
    )

    started = time.perf_counter()
    queries = embed(args.host, args.model, [f"{query_prefix}{q}" for q, _ in PROBES])
    print(f"one live query: {(time.perf_counter() - started) / len(PROBES) * 1000:.0f} ms\n")

    print(f"{'asked (ru/en)':<44} {'rank':>6}  {'cos':>5}  what came back first")
    print("-" * 116)
    hits = 0
    asked = 0
    for (question, expected), query in zip(PROBES, queries):
        if not any(expected.lower() in title.lower() for title in titles):
            print(f"{question[:43]:<44} {'n/a':>6}         (no such task in this database)")
            continue
        asked += 1
        scored = sorted(
            ((sum(a * b for a, b in zip(query, vector)), i) for i, vector in enumerate(vectors)),
            reverse=True,
        )
        rank = next(n + 1 for n, (_, i) in enumerate(scored) if expected.lower() in titles[i].lower())
        if rank <= 5:
            hits += 1
        print(f"{question[:43]:<44} {rank:>6}  {scored[0][0]:.3f}  {titles[scored[0][1]][:54]!r}")

    print(f"\nright task in the top 5: {hits}/{asked}")
    print("for comparison — multilingual-e5-small (local, CPU): 9/10,  nomic-embed-text: 3/10")
    if not asked:
        print("(the probes above are written against the dev database — use --links on any other one)")

    if args.links:
        score_against_links(tasks, links, vectors)
    if args.pairs:
        show_closest_pairs(tasks, vectors, args.pairs)
    return 0 if asked and hits >= asked * 0.8 else 0


if __name__ == "__main__":
    sys.exit(main())
