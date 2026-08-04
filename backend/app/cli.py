"""Operator command line — ``smarttender-admin``.

Deliberately small. Everything here is either a bootstrap step (creating the
default schedules) or a diagnostic that answers a question the dashboard cannot
(``why is this connector producing nothing?``).

``dry-run`` is the one to reach for first when a portal misbehaves: it executes
a connector end to end and prints what it extracted **without writing anything
to the database or to object storage**, which makes it safe to run against
production configuration while debugging a selector.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
def cmd_connectors(args: argparse.Namespace) -> int:
    """List every configured connector and why it is or is not runnable."""
    from app.connectors.registry import get_registry

    registry = get_registry()
    registry.load(force=True)

    rows = [info.to_dict() for info in registry.describe_all()]
    if args.json:
        print(json.dumps({"connectors": rows, "errors": registry.errors()}, indent=2))
        return 0

    print(f"{'KEY':<12} {'AVAILABLE':<10} {'STRATEGY':<9} {'REASON'}")
    print("-" * 72)
    for row in rows:
        print(
            f"{row['key']:<12} {row['available']!s:<10} "
            f"{row['strategy']:<9} {row['unavailable_reason'] or ''}"
        )
    for key, error in registry.errors().items():
        print(f"\n  ! {key}: {error}")
    return 0


def cmd_sync_sources(args: argparse.Namespace) -> int:
    """Reconcile the ``sources`` table with the connector configuration."""
    from app.db.session import session_scope
    from app.services.sources import sync_sources

    with session_scope() as session:
        result = sync_sources(session)
    print(f"Sources synchronised: {result['created']} created, {result['updated']} updated.")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    """Run a connector and print what it extracted, writing nothing."""
    from app.connectors.base import ConnectorContext
    from app.connectors.registry import get_registry
    from app.schemas.filters import TenderFilters

    registry = get_registry()
    registry.load(force=True)

    filters = TenderFilters(
        keywords=args.keyword or [],
        max_pages=args.pages,
        max_results_per_source=args.limit,
    )
    context = ConnectorContext(
        deadline_seconds=float(args.timeout),
        max_items=args.limit,
        max_pages=args.pages,
        allow_private_hosts=True,
    )

    connector = registry.create(args.connector, context)
    outcome = asyncio.run(connector.run(filters))

    print(json.dumps(outcome.to_summary(), indent=2))

    if outcome.item_failures:
        print(f"\n--- {len(outcome.item_failures)} item failure(s) ---")
        for failure in outcome.item_failures[:10]:
            print(f"  {failure.error_type}: {failure.message}")

    print(f"\n--- {outcome.items_found} tender(s) ---")
    for tender in outcome.tenders[: args.show]:
        print(f"\n  title    : {tender.title}")
        print(f"  reference: {tender.reference}")
        print(f"  buyer    : {tender.buyer}")
        print(f"  deadline : {tender.deadline}")
        print(f"  budget   : {tender.estimated_budget} {tender.currency or ''}")
        print(f"  url      : {tender.source_url}")

    # Non-zero exit so CI can gate on a connector that has stopped working.
    return 0 if outcome.succeeded and outcome.items_found else 1


def cmd_score(args: argparse.Namespace) -> int:
    """Explain how a stored tender was scored."""
    import uuid as uuid_module

    from app.db.models.tender import Tender
    from app.db.session import session_scope
    from app.services.scoring import get_scoring_engine
    from app.workers.tasks.pipeline import _to_normalized

    with session_scope() as session:
        tender = session.get(Tender, uuid_module.UUID(args.tender_id))
        if tender is None:
            print(f"No tender with id {args.tender_id}.", file=sys.stderr)
            return 1
        result = get_scoring_engine().score(_to_normalized(tender))

    print(f"score: {result.score:.4f}  band: {result.band.value}")
    print(f"profile: {result.profile_name} v{result.profile_version}\n")
    for criterion, entry in sorted(
        result.breakdown.items(), key=lambda item: item[1]["weighted"], reverse=True
    ):
        value = entry["value"]
        rendered = f"{value:.3f}" if value is not None else "  n/a"
        print(f"  {criterion:<22} {rendered}  x{entry['weight']:<5} = {entry['weighted']:.4f}")
        print(f"    {entry['explanation']}")
    if result.veto_reason:
        print(f"\n  VETOED: {result.veto_reason}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Create the default schedules and a demo notification profile."""
    from sqlalchemy import func, select

    from app.core.enums import RelevanceBand, ScheduleKind
    from app.core.identity import utc_now
    from app.db.models.notification import UserPreference
    from app.db.models.schedule import Schedule, ScheduleChangeSentinel
    from app.db.session import session_scope

    defaults = [
        {
            "name": "tuneps-every-2-hours",
            "description": "Incremental sweep of TUNEPS during business hours.",
            "kind": ScheduleKind.INTERVAL.value,
            "interval_seconds": 7200,
            "connectors": ["tuneps"],
            "filters": {"published_within_days": 7},
        },
        {
            "name": "all-sources-daily",
            "description": "Full daily sweep of every available source.",
            "kind": ScheduleKind.INTERVAL.value,
            "interval_seconds": 86400,
            "connectors": [],
            "filters": {"published_within_days": 30},
        },
    ]

    created = 0
    profile_created = False
    with session_scope() as session:
        for spec in defaults:
            exists = session.execute(
                select(Schedule.id).where(Schedule.name == spec["name"])
            ).first()
            if exists:
                continue
            session.add(Schedule(created_by="bootstrap", queue="scraping", **spec))
            created += 1

        if args.user:
            exists = session.execute(
                select(UserPreference.id).where(UserPreference.user_id == args.user)
            ).first()
            if not exists:
                # Deliberately unrestricted apart from the relevance floor.
                # Seeding sectors=["Technologies de l'information"] reads well
                # and notifies nothing: portal listings carry no sector field,
                # and a title says "logiciel", never that phrase. A default
                # that silently matches zero tenders makes a working notifier
                # look broken. Relevance is already the filter that matters —
                # the user narrows from there.
                session.add(
                    UserPreference(
                        user_id=args.user,
                        email=args.email,
                        display_name=args.user,
                        min_relevance_band=RelevanceBand.RELEVANT.value,
                        channels=["in_app", "email"],
                    )
                )
                profile_created = True

        existing_profiles = session.execute(
            select(func.count(UserPreference.id)).where(UserPreference.active.is_(True))
        ).scalar_one()

        sentinel = session.get(ScheduleChangeSentinel, 1)
        if sentinel is None:
            session.add(ScheduleChangeSentinel(id=1, last_update=utc_now()))
        else:
            sentinel.last_update = utc_now()

    print(f"Seed complete: {created} schedule(s) created.")
    if profile_created:
        print(f"Notification profile created for '{args.user}'.")
    elif not existing_profiles:
        # Notifications are built per active preference. With none, the pipeline
        # runs to completion and announces nothing — which looks exactly like a
        # broken notifier. Say so here rather than let it be discovered later.
        print(
            "\nNo notification profile exists, so NO alerts will be raised for\n"
            "relevant tenders. Create one with:\n"
            "    smarttender-admin seed --user <name> --email <address>\n"
            "or from the Preferences screen in the app."
        )
    return 0


def cmd_capture_login(args: argparse.Namespace) -> int:
    """Open a real browser, let you sign in, and save the session.

    This is the interactive half of the ``browser_session`` auth mode: you log
    in once — including OAuth, MFA, or an anti-bot challenge, all of which a
    headless script handles badly — and the resulting cookies are saved for the
    fast httpx crawl to reuse.

    The browser opens **headed** by default so you can see and complete the
    login yourself.
    """
    from app.connectors.http.session_store import save_session, session_path
    from app.connectors.registry import get_registry

    registry = get_registry()
    registry.load(force=True)
    try:
        config = registry.config(args.connector)
    except Exception as exc:
        print(f"Unknown connector '{args.connector}': {exc}", file=sys.stderr)
        return 1

    auth = config.auth
    login_url = args.url or auth.get("login_url") or config.base_url
    path = session_path(args.connector, auth.get("session_file"))

    async def capture() -> int:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print(
                "Playwright is not installed. Run:\n"
                "  pip install playwright && playwright install chromium",
                file=sys.stderr,
            )
            return 1

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=args.headless)
            context = await browser.new_context(
                locale="fr-FR", ignore_https_errors=config.allow_insecure_tls
            )
            page = await context.new_page()
            print(f"\nOpening {login_url}")
            await page.goto(login_url, wait_until="domcontentloaded")

            print(
                "\n" + "=" * 66 + "\n"
                "  Sign in in the browser window that just opened.\n"
                "  Navigate to the page whose data you want (the search results).\n"
                "  Then come back here and press ENTER to save the session.\n"
                + "=" * 66
            )
            # Blocking input is correct here: this command is interactive by
            # design and must wait for a human to finish logging in.
            await asyncio.get_event_loop().run_in_executor(None, input)

            state = await context.storage_state()
            user_agent = await page.evaluate("navigator.userAgent")
            await browser.close()

        cookie_count = len(state.get("cookies", []))
        if not cookie_count:
            print("No cookies were captured — did the sign-in complete?", file=sys.stderr)
            return 1

        save_session(path, state, headers={"User-Agent": user_agent})
        print(f"\nSaved {cookie_count} cookies to {path}")
        print(f"Verify with:  smarttender-admin dry-run {args.connector} --pages 1")
        return 0

    return asyncio.run(capture())


def cmd_from_curl(args: argparse.Namespace) -> int:
    """Convert a DevTools 'Copy as cURL' into connector configuration.

    Finding a private API is a manual fifteen minutes; turning the captured
    request into config is mechanical, so this does it. Pipe or paste the cURL,
    optionally alongside a saved JSON response, and get a YAML block to drop
    into ``config/connectors/``.
    """
    from pathlib import Path

    from app.connectors.curl_import import parse_curl, suggest_config

    if args.file:
        command = Path(args.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        command = sys.stdin.read()
    else:
        print(
            "Paste the cURL command, then press Ctrl+Z + ENTER (Windows) or "
            "Ctrl+D (Unix):\n",
            file=sys.stderr,
        )
        command = sys.stdin.read()

    try:
        parsed = parse_curl(command)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    sample = None
    if args.response:
        try:
            sample = json.loads(Path(args.response).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read the sample response: {exc}", file=sys.stderr)
            return 1

    print("# ---- what was detected ----", file=sys.stderr)
    for key, value in parsed.describe().items():
        print(f"#   {key}: {value}", file=sys.stderr)
    print("# ---------------------------\n", file=sys.stderr)

    yaml_text = suggest_config(args.connector, parsed, sample)
    if args.out:
        Path(args.out).write_text(yaml_text, encoding="utf-8")
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(yaml_text)
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Probe every infrastructure dependency."""
    from app.db.session import check_sync_connection
    from app.services.storage import get_storage

    checks: dict[str, bool] = {"database": check_sync_connection()}

    try:
        import redis

        from app.core.config import get_settings

        client = redis.Redis.from_url(get_settings().redis.broker_url, socket_timeout=3)
        client.ping()
        client.close()
        checks["broker"] = True
    except Exception:
        checks["broker"] = False

    checks["storage"] = get_storage().health()

    for name, ok in checks.items():
        print(f"  {name:<10} {'ok' if ok else 'UNREACHABLE'}")
    return 0 if all(checks.values()) else 1


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smarttender-admin",
        description="Operator tooling for the SmartTender ingestion platform.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    connectors = subparsers.add_parser("connectors", help="List connectors and availability.")
    connectors.add_argument("--json", action="store_true")
    connectors.set_defaults(func=cmd_connectors)

    sync = subparsers.add_parser("sync-sources", help="Reconcile the sources table.")
    sync.set_defaults(func=cmd_sync_sources)

    dry_run = subparsers.add_parser(
        "dry-run", help="Run a connector and print results without writing anything."
    )
    dry_run.add_argument("connector")
    dry_run.add_argument("--keyword", action="append", help="Repeatable.")
    dry_run.add_argument("--pages", type=int, default=1)
    dry_run.add_argument("--limit", type=int, default=20)
    dry_run.add_argument("--timeout", type=int, default=120)
    dry_run.add_argument("--show", type=int, default=5, help="Tenders to print in full.")
    dry_run.set_defaults(func=cmd_dry_run)

    score = subparsers.add_parser("score", help="Explain a tender's score.")
    score.add_argument("tender_id")
    score.set_defaults(func=cmd_score)

    seed = subparsers.add_parser("seed", help="Create default schedules.")
    seed.add_argument(
        "--user",
        help=(
            "Also create a notification profile for this user id. Must match the "
            "identity the UI sends as X-User-Id (default: 'operator'), or the "
            "Notifications screen stays empty while alerts accumulate elsewhere."
        ),
    )
    seed.add_argument("--email", default=None)
    seed.set_defaults(func=cmd_seed)

    health = subparsers.add_parser("health", help="Probe infrastructure dependencies.")
    health.set_defaults(func=cmd_health)

    capture = subparsers.add_parser(
        "capture-login",
        help="Open a browser, sign in interactively, and save the session for crawling.",
    )
    capture.add_argument("connector")
    capture.add_argument("--url", help="Override the login URL from the connector config.")
    capture.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible window (only useful if no interaction is needed).",
    )
    capture.set_defaults(func=cmd_capture_login)

    from_curl = subparsers.add_parser(
        "from-curl",
        help="Convert a DevTools 'Copy as cURL' into connector YAML.",
    )
    from_curl.add_argument("connector", help="Connector key the config is for.")
    from_curl.add_argument("--file", help="Read the cURL from a file instead of stdin.")
    from_curl.add_argument(
        "--response", help="Path to a saved JSON response, to infer the field mapping."
    )
    from_curl.add_argument("--out", help="Write the YAML here instead of stdout.")
    from_curl.set_defaults(func=cmd_from_curl)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.error("cli.failed", command=args.command, error=str(exc))
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
