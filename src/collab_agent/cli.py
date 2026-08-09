from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

from .agent_worker import AgentWorker
from .ai_evaluation import build_ai_p0_report
from .evaluation import run_p0_evaluation
from .extraction import extract_file
from .meeting import load_meeting_service
from .models import read_text_file
from .service import CoordinationService, load_fixture
from .store import Database
from .web import serve_dashboard


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collab-agent", description="P0 multi-coworker coordination Agent"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("eval", help="run the deterministic P0 scenario")
    evaluate.add_argument("--db", default="var/p0.sqlite3")
    evaluate.add_argument("--fixture", default="fixtures/p0_weekly.json")
    evaluate.add_argument("--report", default="var/report.json")
    evaluate.add_argument(
        "--postgres",
        action="store_true",
        help="use DATABASE_URL from .env.local instead of SQLite",
    )
    evaluate.add_argument(
        "--fresh", action="store_true", help="replace only the selected evaluation DB"
    )
    ai_evaluate = subparsers.add_parser(
        "eval-ai-p0",
        help="run the offline interview-focused AI engineering Harness",
    )
    ai_evaluate.add_argument("--db", default="var/ai-p0.sqlite3")
    ai_evaluate.add_argument("--fixture", default="fixtures/p0_weekly.json")
    ai_evaluate.add_argument(
        "--extraction-cases",
        default="fixtures/ai_p0_extraction_cases.json",
    )
    ai_evaluate.add_argument("--report", default="var/ai-p0-report.json")
    ai_evaluate.add_argument("--fresh", action="store_true")
    serve = subparsers.add_parser("serve", help="serve the local P0 workbench")
    serve.add_argument("--db", default="var/p0.sqlite3")
    serve.add_argument("--fixture", default="fixtures/p0_weekly.json")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--result-processing",
        choices=("bailian", "local", "disabled"),
        default="local",
        help="deployment-level automatic final organization policy",
    )
    serve.add_argument(
        "--postgres",
        action="store_true",
        help="serve the workbench from DATABASE_URL in .env.local",
    )
    extract = subparsers.add_parser(
        "extract", help="extract action-item candidates with Bailian"
    )
    extract.add_argument("--input", required=True)
    extract.add_argument("--output", default="var/extractions/latest.json")
    extract.add_argument("--model", default=None)
    extract.add_argument("--meeting-date", default=None)
    extract.add_argument(
        "--tools",
        action="store_true",
        help=(
            "let the model look quotes up in the transcript before citing them; "
            "uses a separately versioned prompt and costs extra rounds"
        ),
    )
    meeting = subparsers.add_parser(
        "serve-meeting", help="import extracted action items and serve the collaboration workbench"
    )
    meeting.add_argument("--extraction", required=True)
    meeting.add_argument("--transcript", required=True)
    meeting.add_argument("--organization", required=True)
    meeting.add_argument("--coordinator", default="会议负责人")
    meeting.add_argument(
        "--participant",
        action="append",
        default=[],
        help="meeting participant display name; repeat for each participant",
    )
    meeting.add_argument("--db", default="var/meeting.sqlite3")
    meeting.add_argument("--host", default="127.0.0.1")
    meeting.add_argument("--port", type=int, default=8766)
    meeting.add_argument(
        "--result-processing",
        choices=("bailian", "local", "disabled"),
        default="local",
        help="deployment-level automatic final organization policy",
    )
    meeting.add_argument(
        "--postgres",
        action="store_true",
        help="use DATABASE_URL from .env.local instead of SQLite",
    )
    agent = subparsers.add_parser(
        "agent-meeting",
        help="run the durable Agent Worker for one imported meeting",
    )
    agent.add_argument("--extraction", required=True)
    agent.add_argument("--transcript", required=True)
    agent.add_argument("--organization", required=True)
    agent.add_argument("--coordinator", default="会议负责人")
    agent.add_argument(
        "--participant",
        action="append",
        default=[],
        help="meeting participant display name; repeat for each participant",
    )
    agent.add_argument("--db", default="var/meeting.sqlite3")
    agent.add_argument(
        "--result-processing",
        choices=("bailian", "local"),
        default="local",
        help="semantic processing policy used by the independent Agent Worker",
    )
    agent.add_argument(
        "--postgres",
        action="store_true",
        help="use DATABASE_URL from .env.local instead of SQLite",
    )
    agent.add_argument(
        "--allow-contribution-analysis",
        action="store_true",
        help=(
            "allow validated but not yet owner-handled collaborator content "
            "to enter semantic processing"
        ),
    )
    run_mode = agent.add_mutually_exclusive_group()
    run_mode.add_argument("--once", action="store_true")
    run_mode.add_argument("--until-idle", action="store_true")
    agent.add_argument("--max-steps", type=int, default=100)
    agent.add_argument(
        "--notify",
        choices=("mock", "feishu"),
        default="mock",
        help=(
            "where human-facing Outbox effects are delivered; feishu needs "
            "FEISHU_APP_ID/FEISHU_APP_SECRET and bound open_ids"
        ),
    )
    agent.add_argument("--poll-seconds", type=float, default=2.0)
    product = subparsers.add_parser(
        "eval-product",
        help="deterministic product metrics: human cost, citations, tokens, gates",
    )
    product.add_argument("--db", default="var/p0.sqlite3")
    product.add_argument("--postgres", action="store_true")
    product.add_argument("--episode-id", default="")
    product.add_argument("--report", default="var/product-evaluation.json")

    extraction_eval = subparsers.add_parser(
        "eval-extraction",
        help="score extraction against labelled meetings, with baselines",
    )
    extraction_eval.add_argument(
        "--cases",
        default="fixtures/ai_p0_extraction_cases.json",
        help="project-annotated meetings (JSON) or AMC-A JSONL via --amc-a",
    )
    extraction_eval.add_argument(
        "--amc-a", default="", help="AMC-A style JSONL with sentence labels"
    )
    extraction_eval.add_argument(
        "--alimeeting4mug",
        default="",
        help="root of the released AliMeeting4MUG dataset (contains data/*.zip)",
    )
    extraction_eval.add_argument("--split", default="dev")
    extraction_eval.add_argument(
        "--limit", type=int, default=0, help="score only the first N meetings"
    )
    extraction_eval.add_argument(
        "--predictions",
        default="",
        help="replay a stored extraction run instead of calling a model",
    )
    extraction_eval.add_argument(
        "--with-single-prompt",
        action="store_true",
        help="also run the one-shot prompt baseline (consumes tokens)",
    )
    extraction_eval.add_argument(
        "--with-project-chain",
        action="store_true",
        help="also run this project's full extraction chain (consumes tokens)",
    )
    extraction_eval.add_argument(
        "--with-project-chain-tools",
        action="store_true",
        help=(
            "also run the chain on the tool-calling prompt; pair with "
            "--with-project-chain to score both on one corpus (consumes tokens)"
        ),
    )
    extraction_eval.add_argument(
        "--report", default="var/extraction-evaluation.json"
    )

    annotation = subparsers.add_parser(
        "check-annotation",
        help="validate a hand-annotated meeting file before using it as truth",
    )
    annotation.add_argument("--cases", required=True)

    gold_extract = subparsers.add_parser(
        "gold-to-extraction",
        help=(
            "derive an extraction file from a validated annotation, so a demo "
            "loads the same meeting every time without calling a model"
        ),
    )
    gold_extract.add_argument("--gold", required=True)
    gold_extract.add_argument("--output", required=True)
    gold_extract.add_argument(
        "--case-id", default="", help="required when the file holds several cases"
    )

    intake = subparsers.add_parser(
        "feishu-intake",
        help=(
            "pull a meeting transcript and roster proposal out of Feishu minutes"
        ),
    )
    intake.add_argument(
        "--minute-token",
        required=True,
        help="妙记链接末段的 token",
    )
    intake.add_argument(
        "--chat-id",
        default="",
        help="群 ID；给了才能提议参会名单并带出 open_id",
    )
    intake.add_argument(
        "--output",
        default="",
        help="逐字稿写到哪个文件；不给则只打印统计",
    )
    intake.add_argument("--file-format", default="srt", choices=("srt", "txt"))

    link = subparsers.add_parser(
        "link",
        help="propose, list and decide links to action items from earlier meetings",
    )
    link.add_argument(
        "action",
        choices=("propose", "list", "confirm", "reject"),
    )
    link.add_argument("--db", default="var/meeting.sqlite3")
    link.add_argument("--postgres", action="store_true")
    link.add_argument(
        "--episode-id",
        default="",
        help="defaults to the most recently created episode",
    )
    link.add_argument(
        "--actor",
        default="",
        help=(
            "whose history may be searched; the pool is limited to earlier "
            "meetings this person attended. Accepts a display name."
        ),
    )
    link.add_argument("--link-id", default="", help="for confirm / reject")
    link.add_argument(
        "--with-model",
        action="store_true",
        help=(
            "also ask Bailian for links the deterministic floor cannot see "
            "(consumes tokens)"
        ),
    )
    link.add_argument(
        "--with-embeddings",
        action="store_true",
        help=(
            "score candidates semantically as well as lexically; results are "
            "cached by content, so a repeated run is free"
        ),
    )
    link.add_argument(
        "--show-scores",
        action="store_true",
        help="print every candidate with both scores, not only the proposals",
    )
    link.add_argument(
        "--plain",
        action="store_true",
        help=(
            "print a table with short ids instead of JSON; the ids are ready "
            "to paste into confirm/reject"
        ),
    )

    bind = subparsers.add_parser(
        "feishu-bind",
        help="bind one meeting participant to their Feishu open_id",
    )
    bind.add_argument("--db", default="var/feishu-meeting.sqlite3")
    bind.add_argument("--actor", help="internal actor id / participant name")
    bind.add_argument("--open-id", help="Feishu open_id, starts with ou_")
    bind.add_argument("--display-name", default="")
    bind.add_argument(
        "--list", action="store_true", help="list current bindings and exit"
    )
    bind.add_argument(
        "--unbind",
        action="store_true",
        help="remove the binding for --actor instead of creating one",
    )
    bind.add_argument("--postgres", action="store_true")
    serve_feishu = subparsers.add_parser(
        "feishu-serve",
        help="run one imported meeting with Feishu as the interaction surface",
    )
    serve_feishu.add_argument("--extraction", required=True)
    serve_feishu.add_argument("--transcript", required=True)
    serve_feishu.add_argument("--organization", required=True)
    serve_feishu.add_argument("--coordinator", default="会议负责人")
    serve_feishu.add_argument(
        "--participant",
        action="append",
        default=[],
        help="meeting participant display name; repeat for each participant",
    )
    serve_feishu.add_argument("--db", default="var/feishu-meeting.sqlite3")
    serve_feishu.add_argument("--postgres", action="store_true")
    serve_feishu.add_argument(
        "--dispatch-seconds",
        type=float,
        default=2.0,
        help="how often the Outbox is drained to Feishu",
    )
    serve_feishu.add_argument(
        "--dry-run",
        action="store_true",
        help="use the offline transport; nothing reaches the tenant",
    )
    return parser


def _database_url_from_local_env(path: str | Path = ".env.local") -> str:
    """Read DATABASE_URL from the one local env file.

    Shares `read_local_env` with the Feishu config so both settle comments and
    whitespace identically. DATABASE_URL deliberately does not fall back to the
    process environment the way the Feishu keys do: the workbench and the Agent
    Worker must agree on one database, and an ambient DATABASE_URL from another
    project is exactly the kind of accident that would split them.
    """

    from .feishu_config import read_local_env

    env_path = Path(path)
    if not env_path.exists():
        raise FileNotFoundError(
            ".env.local is missing; run scripts/setup_postgres.ps1 first"
        )
    values = read_local_env(env_path)
    if "DATABASE_URL" not in values:
        found = ", ".join(sorted(values)) or "nothing"
        raise ValueError(
            f"DATABASE_URL is missing from {path}; it currently defines {found}"
        )
    return values["DATABASE_URL"]


def _load_meeting_from_args(args: argparse.Namespace) -> tuple[Database, CoordinationService]:
    if args.postgres:
        from .postgres_store import PostgresDatabase

        database = PostgresDatabase(
            _database_url_from_local_env(),
            schema_path=Path("db/postgres_schema.sql").resolve(),
        )
    else:
        database = Database(Path(args.db).resolve())
    database.initialize()
    im = None
    # The dispatcher only knows the EffectId contract, so swapping the
    # transport changes where an effect lands and nothing else. Domain state
    # is read from the domain tables, so the web workbench is unaffected.
    if getattr(args, "notify", "mock") == "feishu":
        _, im = _build_feishu_im(database, dry_run=False)
    service = load_meeting_service(
        database,
        extraction_path=args.extraction,
        transcript_path=args.transcript,
        organization_name=args.organization,
        coordinator_name=args.coordinator,
        participant_names=args.participant,
        im=im,
    )
    _catch_clock_up(service)
    return database, service


def _catch_clock_up(service: CoordinationService) -> None:
    """Bring a live meeting's clock up to the wall clock.

    `now()` reads `episodes.current_sim_time`, which is written once when the
    meeting is imported and then only by an explicit advance. Nothing advanced
    it while serving, so a meeting imported on Tuesday still believed it was
    Tuesday on Friday: deadlines never arrived, the schedule strip drew "now"
    days behind every bar, and the header stated a date that was simply wrong.

    Evaluation keeps the virtual clock -- that is what makes a run
    reproducible -- so this belongs here, on the path that serves real people,
    rather than in `now()` where it would move under the metrics. It is a
    catch-up at startup, not a running clock: REAL_SCHEDULER in
    capabilities.json is still NOT_DONE, and nothing fires deadlines on its own
    yet.
    """

    from .models import iso_time, parse_time  # noqa: PLC0415 - local by design

    real_now = datetime.now(UTC)
    if parse_time(service.now()) >= real_now:
        return
    service.advance_time(iso_time(real_now))


def _database_factory(
    args: argparse.Namespace, *, allow_cross_thread: bool = False
) -> "Callable[[], Database]":
    """Build a zero-argument opener for this command's database.

    Kept separate from `_open_database` because the Feishu runtime needs to
    open one connection per thread, not reuse a single shared one.
    """

    if getattr(args, "postgres", False):
        from .postgres_store import PostgresDatabase

        url = _database_url_from_local_env()
        schema_path = Path("db/postgres_schema.sql").resolve()
        return lambda: PostgresDatabase(url, schema_path=schema_path)
    resolved = Path(args.db).resolve()
    return lambda: Database(resolved, allow_cross_thread=allow_cross_thread)


def _open_database(args: argparse.Namespace) -> Database:
    database = _database_factory(args)()
    database.initialize()
    return database


def _resolve_actor(database: Database, actor: str) -> str:
    """Accept a display name where an actor id is wanted.

    Every authorisation check keys on the internal id, so the id is what gets
    stored; resolving here just lets a person type the name they know.
    """

    row = database.one(
        "SELECT actor_id FROM actors WHERE display_name = ?", (actor,)
    )
    return dict(row)["actor_id"] if row else actor


def _build_feishu_im(database: object, *, dry_run: bool):
    from .feishu_config import load_feishu_config
    from .feishu_im import FeishuIM, LarkTransport, RecordingTransport

    if dry_run:
        # No credentials are needed to prove the wiring, so a dry run must not
        # demand them.
        config = None
        transport = RecordingTransport()
    else:
        config = load_feishu_config()
        transport = LarkTransport(config)
    return config, FeishuIM(database, transport)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "feishu-bind":
        database = _open_database(args)
        try:
            from .feishu_im import FeishuIM, RecordingTransport

            # Binding never sends anything, so the offline transport keeps this
            # command usable before credentials exist.
            im = FeishuIM(database, RecordingTransport())
            if args.unbind:
                if not args.actor:
                    print("--actor is required to unbind")
                    return 2
                removed = im.unbind_actor(args.actor)
                print(
                    json.dumps(
                        {"unbound": args.actor, "removed": removed},
                        ensure_ascii=False,
                    )
                )
                return 0
            if args.list or not (args.actor and args.open_id):
                if not args.list:
                    print("--actor and --open-id are both required to bind")
                print(
                    json.dumps(
                        {"bindings": im.bindings()}, ensure_ascii=False, indent=2
                    )
                )
                return 0 if args.list else 2
            from .feishu_app import real_now

            # Every coordination command authorises against the internal
            # actor_id, not the name on screen. Resolving here means a person
            # can type the name they know while the binding still keys on the
            # id the domain checks.
            matched = database.one(
                "SELECT actor_id, display_name FROM actors WHERE display_name = ?",
                (args.actor,),
            )
            if matched:
                matched = dict(matched)
                actor_id = matched["actor_id"]
                display_name = args.display_name or matched["display_name"]
                resolution = "resolved_from_display_name"
            else:
                actor_id = args.actor
                display_name = args.display_name
                resolution = "stored_verbatim_no_matching_actor"
            im.bind_actor(
                actor_id,
                args.open_id,
                display_name=display_name,
                sim_time=real_now(),
            )
            print(
                json.dumps(
                    {
                        "bound_actor_id": actor_id,
                        "display_name": display_name,
                        "open_id": args.open_id,
                        "resolution": resolution,
                        "database": str(Path(args.db).resolve())
                        if not args.postgres
                        else "postgres",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if resolution == "stored_verbatim_no_matching_actor":
                print(
                    "note: no participant with that display name exists in this "
                    "database yet. If you meant a meeting participant, load the "
                    "meeting first and re-run against the same --db.",
                )
            return 0
        finally:
            database.close()
    if args.command == "feishu-intake":
        from .feishu_config import load_feishu_config
        from .feishu_minutes import LarkMinutesTransport, MinutesError, intake

        try:
            result = intake(
                LarkMinutesTransport(load_feishu_config()),
                minute_token=args.minute_token,
                chat_id=args.chat_id,
                file_format=args.file_format,
            )
        except MinutesError as error:
            print(str(error))
            return 1

        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(result["transcript"], encoding="utf-8")

        roster = result["roster"]
        matched = roster["spoke_and_in_chat"]
        print(f"逐字稿 {result['line_count']} 行，{len(result['speakers'])} 位发言人")
        if args.output:
            print(f"已写入 {Path(args.output).resolve()}")
        print()
        if matched:
            print("发言且在群里（可直接作为参会人）：")
            for row in matched:
                print(f"   {row['name']:<12} {row['open_id']}")
        if roster["spoke_but_not_in_chat"]:
            print("发言但不在群里（可能是转写变体或外部人员，需你判断）：")
            for name in roster["spoke_but_not_in_chat"]:
                print(f"   {name}")
        if roster["in_chat_but_silent"]:
            print("在群里但全程未发言（可能到场未说话，也可能根本没参会）：")
            for name in roster["in_chat_but_silent"]:
                print(f"   {name}")
        if matched:
            # Printed rather than executed: the roster is the authorisation
            # boundary, and "who is in the group chat" is not the same question
            # as "who attended". Removing the typing is worth doing; removing
            # the decision is not.
            flags = " ".join(f'--participant "{row["name"]}"' for row in matched)
            print()
            print("确认名单后，载入会议：")
            print(f"   ... {flags} --postgres")
            print("绑定飞书身份（每人一条）：")
            for row in matched:
                print(
                    f'   collab-agent feishu-bind --postgres --actor "{row["name"]}" '
                    f'--open-id {row["open_id"]}'
                )
        return 0
    if args.command == "gold-to-extraction":
        from .demo_fixtures import gold_to_extraction

        try:
            result = gold_to_extraction(
                args.gold, args.output, case_id=args.case_id or None
            )
        except ValueError as error:
            print(str(error))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "link":
        from .feishu_app import real_now
        from .linkage import (
            LinkageError,
            bailian_completer,
            decide_link,
            ensure_schema,
            links_for,
            propose_for_action_item,
            resolve_link_id,
        )

        database = _open_database(args)
        try:
            ensure_schema(database)
            episode_id = args.episode_id
            if not episode_id:
                row = database.one(
                    "SELECT episode_id FROM episodes "
                    "ORDER BY created_sim_time DESC, episode_id LIMIT 1"
                )
                if not row:
                    print("no episodes in this database")
                    return 1
                episode_id = dict(row)["episode_id"]
            run_row = database.one(
                "SELECT run_id FROM episodes WHERE episode_id = ?", (episode_id,)
            )
            run_id = dict(run_row)["run_id"] if run_row else "run"

            if args.action in {"confirm", "reject"}:
                if not args.link_id:
                    print("--link-id is required to confirm or reject")
                    return 2
                if not args.actor:
                    print("--actor is required: a decision records who made it")
                    return 2
                actor_id = _resolve_actor(database, args.actor)
                try:
                    link_id = resolve_link_id(database, args.link_id)
                    outcome = decide_link(
                        database,
                        run_id=run_id,
                        link_id=link_id,
                        approve=args.action == "confirm",
                        actor_id=actor_id,
                        sim_time=real_now(),
                    )
                except LinkageError as error:
                    print(str(error))
                    return 1
                print(json.dumps(outcome, ensure_ascii=False, indent=2))
                return 0

            items = [
                dict(row)
                for row in database.all(
                    "SELECT * FROM action_items WHERE episode_id = ? "
                    "ORDER BY created_sim_time, action_item_id",
                    (episode_id,),
                )
            ]

            if args.action == "list":
                report = []
                for item in items:
                    links = links_for(
                        database, action_item_id=item["action_item_id"]
                    )
                    for link in links:
                        prior = database.one(
                            "SELECT title, episode_id FROM action_items "
                            "WHERE action_item_id = ?",
                            (link["prior_action_item_id"],),
                        )
                        report.append(
                            {
                                "link_id": link["link_id"],
                                "status": link["status"],
                                "relation": link["relation"],
                                "source": link["source"],
                                "this_task": item["title"],
                                "prior_task": dict(prior)["title"] if prior else "?",
                                "prior_meeting": (
                                    dict(prior)["episode_id"] if prior else "?"
                                ),
                                "reason": link["reason"],
                            }
                        )
                if args.plain:
                    if not report:
                        print("这个会议还没有关联提议。")
                        print(
                            "先跑：link propose --db ... --actor <姓名> --with-model"
                        )
                        return 0
                    # Short ids, because the next command needs one and nobody
                    # should be retyping a uuid mid-demo.
                    print(f"{'ID':<12} {'状态':<10} {'关系':<13} 任务")
                    for row in report:
                        print(
                            f"{row['link_id'][:10]:<12} {row['status']:<10} "
                            f"{row['relation']:<13} {row['this_task']}"
                        )
                        print(f"{'':<12} {'':<10} {'':<13} ← {row['prior_task']}")
                    print()
                    print(
                        "确认：link confirm --db <库> --link-id "
                        f"{report[0]['link_id'][:10]} --actor <姓名>"
                    )
                    return 0
                print(
                    json.dumps(
                        {"episode_id": episode_id, "links": report},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0

            if not args.actor:
                print(
                    "--actor is required: the searchable history is limited to "
                    "meetings that person attended"
                )
                return 2
            actor_id = _resolve_actor(database, args.actor)
            complete = bailian_completer() if args.with_model else None

            embed = None
            cached_embedder = None
            if args.with_embeddings or args.show_scores:
                from .embeddings import BailianEmbedder, CachedEmbedder

                cached_embedder = CachedEmbedder(
                    database, BailianEmbedder(), sim_time=real_now()
                )
                embed = cached_embedder.embed

            results = []
            scoreboard = []
            for item in items:
                outcome = propose_for_action_item(
                    database,
                    run_id=run_id,
                    episode_id=episode_id,
                    action_item=item,
                    actor_id=actor_id,
                    sim_time=real_now(),
                    complete=complete,
                    embed=embed,
                )
                if outcome["proposals"]:
                    results.append({"title": item["title"], **outcome})
                if args.show_scores and outcome.get("ranked"):
                    scoreboard.append(
                        {
                            "task": item["title"],
                            "candidates": [
                                {
                                    "prior_task": row["title"],
                                    "lexical": row["lexical_similarity"],
                                    "semantic": row["semantic_similarity"],
                                }
                                for row in outcome["ranked"]
                            ],
                        }
                    )
            report = {
                "episode_id": episode_id,
                "actor_id": actor_id,
                "used_model": bool(complete),
                "used_embeddings": bool(embed),
                "action_items_scanned": len(items),
                "with_proposals": results,
            }
            if cached_embedder is not None:
                report["embedding_cache"] = {
                    "hits": cached_embedder.hits,
                    "misses": cached_embedder.misses,
                }
            if scoreboard:
                report["scoreboard"] = scoreboard
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        finally:
            database.close()
    if args.command == "feishu-serve":
        import threading

        from .feishu_app import FeishuApp, flushing_log, real_now
        from .feishu_commands import AssignmentBridge
        from .feishu_config import FeishuConfig
        from .feishu_notifier import AssignmentNotifier
        from .meeting import load_meeting_service
        from .thread_local_store import ThreadLocalDatabase

        factory = _database_factory(args, allow_cross_thread=True)
        bootstrap = factory()
        bootstrap.initialize()
        bootstrap.close()
        database = ThreadLocalDatabase(factory)
        stop = threading.Event()
        try:
            config, im = _build_feishu_im(database, dry_run=args.dry_run)
            # Injecting FeishuIM here is the whole point: every dispatch the
            # domain enqueues now leaves through Feishu instead of the mock.
            service = load_meeting_service(
                database,
                extraction_path=args.extraction,
                transcript_path=args.transcript,
                organization_name=args.organization,
                coordinator_name=args.coordinator,
                participant_names=args.participant,
                im=im,
            )
            _catch_clock_up(service)
            bridge = AssignmentBridge(service, log=flushing_log)
            notifier = AssignmentNotifier(service, im, log=flushing_log)
            app = FeishuApp(
                config or FeishuConfig(app_id="dry-run", app_secret="dry-run"),
                im,
                episode_id=service.episode_id,
                on_action=lambda record: bridge.handle(record),
            )

            session_id = f"feishu_dispatch_{real_now()}"

            def dispatch_loop() -> None:
                """Push pending assignments and drain the Outbox, on this thread.

                Two sources, because the domain has two: assignment responses
                are pull-based and need projecting into cards, while reminders
                and approvals really do arrive through the Outbox.
                """

                service.recover_dispatcher(session_id)
                while not stop.wait(args.dispatch_seconds):
                    try:
                        outcome = notifier.notify_once()
                        for skip in outcome["skipped"]:
                            if not skip["first_report"]:
                                continue
                            # Said once, and said as the blocker it is: the
                            # task cannot leave PENDING_ASSIGNMENT until every
                            # assignee responds, so an unbound one stalls it.
                            flushing_log(
                                f"[feishu] {skip['display_name']} 尚未绑定飞书，"
                                "卡片发不出去；该任务会一直停在待响应。"
                                f"绑定：feishu-bind --actor \"{skip['display_name']}\" "
                                "--open-id ou_xxx"
                            )
                        delivered = service.dispatch_all(session_id=session_id)
                        if delivered:
                            flushing_log(
                                f"[feishu] dispatched {delivered} outbox entries"
                            )
                    except Exception as error:  # noqa: BLE001 - loop must survive
                        flushing_log(f"[feishu] dispatch failed: {error!r}")

            dispatcher = threading.Thread(
                target=dispatch_loop, name="feishu-dispatch", daemon=True
            )
            dispatcher.start()
            flushing_log(
                f"[feishu] serving episode {service.episode_id}; "
                f"bindings={len(im.bindings())}"
            )
            if args.dry_run:
                flushing_log("[feishu] dry run: not opening a connection")
                stop.set()
                dispatcher.join(timeout=5)
                return 0
            app.run()
            return 0
        except KeyboardInterrupt:
            return 0
        finally:
            stop.set()
            database.close()
    if args.command == "extract":
        result = extract_file(
            args.input,
            args.output,
            model=args.model,
            meeting_date=args.meeting_date,
            use_tools=args.tools,
        )
        print(
            json.dumps(
                {
                    "output": str(Path(args.output).resolve()),
                    "provider": result["provider"],
                    "model": result["model"],
                    "summary": result["summary"],
                    "usage": result["usage"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "serve-meeting":
        database, service = _load_meeting_from_args(args)
        print(
            f"Meeting collaboration workbench: http://{args.host}:{args.port} "
            f"({service.episode_id})"
        )
        try:
            serve_dashboard(
                service,
                host=args.host,
                port=args.port,
                result_processing_mode=args.result_processing,
            )
        finally:
            database.close()
        return 0
    if args.command == "agent-meeting":
        database, service = _load_meeting_from_args(args)
        worker = AgentWorker(
            service,
            processing_mode=args.result_processing,
            allow_contribution_analysis=args.allow_contribution_analysis,
        )
        try:
            print(json.dumps({"recovery": worker.recover()}, ensure_ascii=False))
            if args.once:
                print(json.dumps(worker.run_once(), ensure_ascii=False))
            elif args.until_idle:
                result = worker.run_until_idle(max_steps=args.max_steps)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if result["status"] != "IDLE":
                    return 1
            else:
                print(
                    json.dumps(
                        {
                            "status": "RUNNING",
                            "session_id": worker.session_id,
                            "mode": worker.processing_mode,
                            "poll_seconds": args.poll_seconds,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                worker.run_forever(
                    poll_seconds=args.poll_seconds,
                    on_step=lambda result: print(
                        json.dumps(result, ensure_ascii=False), flush=True
                    ),
                )
        except KeyboardInterrupt:
            return 0
        finally:
            database.close()
        return 0
    if args.command == "check-annotation":
        from .annotation_check import check_annotation_file

        report = check_annotation_file(args.cases)
        for problem in report["problems"]:
            where = f'[{problem["case"]}'
            if problem["item"] is not None:
                where += f' 条目#{problem["item"]}'
            where += "]"
            print(f'{problem["level"]:<7} {where} {problem["message"]}')
        print()
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        if report["valid"]:
            print(
                f'\n通过：{report["summary"]["annotated_items"]} 条标注可用'
                f'（{report["warning_count"]} 条提醒）'
            )
            return 0
        print(f'\n未通过：{report["error_count"]} 个错误必须修复')
        return 1
    if args.command == "eval-product":
        from .product_evaluation import build_product_evaluation

        if args.postgres:
            from .postgres_store import PostgresDatabase

            database = PostgresDatabase(
                _database_url_from_local_env(),
                schema_path=Path("db/postgres_schema.sql").resolve(),
            )
        else:
            database = Database(Path(args.db).resolve())
        database.initialize()
        try:
            episode_id = args.episode_id
            if not episode_id:
                row = database.one(
                    "SELECT episode_id FROM episodes ORDER BY created_sim_time DESC "
                    "LIMIT 1"
                )
                if not row:
                    print("no episode found in this database")
                    return 1
                episode_id = row["episode_id"]
            report = build_product_evaluation(database, episode_id=episode_id)
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        finally:
            database.close()
    if args.command == "eval-extraction":
        from .extraction_baselines import (
            keyword_extractor,
            project_chain_extractor,
            replay_extractor,
            single_prompt_extractor,
        )
        from .extraction_evaluation import (
            compare_extractors,
            load_alimeeting4mug,
            load_amc_a,
            load_project_cases,
        )

        if args.alimeeting4mug:
            meetings = load_alimeeting4mug(
                args.alimeeting4mug,
                split=args.split,
                limit=args.limit or None,
            )
        elif args.amc_a:
            meetings = load_amc_a(args.amc_a)
        else:
            meetings = load_project_cases(args.cases)
        if not meetings:
            print("no labelled meetings were loaded")
            return 1
        # The zero-model floor always runs: it costs nothing and any extractor
        # that cannot beat it is not earning its token spend.
        extractors = {"keyword_floor": keyword_extractor}
        if args.predictions:
            stored = json.loads(
                read_text_file(args.predictions)
            )
            extractors["stored_run"] = replay_extractor(stored)
        if args.with_single_prompt:
            extractors["single_prompt_baseline"] = single_prompt_extractor()
        if args.with_project_chain:
            extractors["project_chain"] = project_chain_extractor()
        if args.with_project_chain_tools:
            extractors["project_chain_tools"] = project_chain_extractor(
                use_tools=True
            )
        report = compare_extractors(meetings, extractors)
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary = {
            name: {
                "sentence_f1": result["sentence_level_positive_f1"]["f1"],
                "item_f1": result["item_level_detection"]["f1"],
                "quote_grounding_rate": result["quote_grounding_rate"],
            }
            for name, result in report["results"].items()
        }
        print(
            json.dumps(
                {
                    "corpus": report["corpus"],
                    "published_reference": report["published_reference"],
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "eval-ai-p0":
        database_path = Path(args.db).resolve()
        if args.fresh and database_path.exists():
            database_path.unlink()
        workflow_report, service = run_p0_evaluation(
            database_path,
            args.fixture,
            fresh=args.fresh,
        )
        try:
            report = build_ai_p0_report(
                service,
                workflow_report,
                extraction_cases_path=args.extraction_cases,
            )
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        finally:
            service.db.close()
    if args.command == "serve":
        fixture = load_fixture(args.fixture)
        if args.postgres:
            from .postgres_store import PostgresDatabase

            database = PostgresDatabase(
                _database_url_from_local_env(),
                schema_path=Path("db/postgres_schema.sql").resolve(),
            )
        else:
            database = Database(Path(args.db).resolve())
        database.initialize()
        service = CoordinationService(database, fixture)
        service.bootstrap()
        print(f"P0 workbench: http://{args.host}:{args.port}")
        try:
            serve_dashboard(
                service,
                host=args.host,
                port=args.port,
                result_processing_mode=args.result_processing,
            )
        finally:
            database.close()
        return 0
    if args.command != "eval":
        return 2
    database_path = Path(args.db).resolve()
    if args.fresh and not args.postgres and database_path.exists():
        database_path.unlink()
    if args.postgres:
        from .postgres_store import database_url_for_schema

        database_url = database_url_for_schema(
            _database_url_from_local_env(), "colwork_evaluation", create=True
        )
    else:
        database_url = None
    report, service = run_p0_evaluation(
        database_path,
        args.fixture,
        database_url=database_url,
        fresh=args.fresh,
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    service.db.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1
