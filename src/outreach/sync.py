"""Re-sync scraper evidence into outreach.db — the missing arrow (2026-11-07).

WHY: the importer is correct (it refreshes `data_json` for rows that already
exist), but it only runs when a scraper RUN is ingested. The scraper meanwhile
keep enriching existing leads, and today's `--refresh-fields
review_samples,rating_breakdown,description,service_options` pass wrote 429
leads' review bodies AFTER the last full import. Measured consequence:
`outreach.db` held 6 leads with reviews and 15 with menus while the scraper held
429 and 33 — so the sender was choosing dishes from star ratings while the dish
evidence sat one folder away, and `message_log` shows 0 of 122 sends ever carried
a dish.

`sync_evidence` is that arrow, and nothing else: refresh every lead from the
scraper read-only, then annotate dishes deterministically. No ICP, no routing, no
sends, no model, so it is safe to run while the queue is PAUSED (annotation is
not dispatch).

RUN SCOPE (2026-11-07): the scraper's re-enrichment is already run-scoped
(`--re-enrich "<run_id>" --refresh-fields review_samples,...`), so the evidence
for a given pass belongs to that run. `--run <run_id>` confines the refresh AND
the dish pass to that run, which is what "assign the enriched data to a scraper
run" means in practice: one run in, one run's leads personalized, everything
traceable to the pass that produced it.

Usage: python -m outreach.sync [--db path] [--scraper path] [--run <run_id>]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from outreach.dishes import ExtractReport, extract_dishes
from outreach.importer import ImportReport, refresh_leads
from outreach.menu_sync import MenuReport, import_menu_items
from outreach.scraper_adapter import SCRAPER_DB
from outreach.store import Database

if TYPE_CHECKING:
    from outreach.store import Database as DatabaseType

DEFAULT_DB = Path(__file__).resolve().parents[2] / "outreach.db"


@dataclass(frozen=True)
class SyncReport:
    imported: ImportReport
    dishes: ExtractReport
    menus: MenuReport | None = None
    run_id: str | None = None

    def as_line(self) -> str:
        scope = f"sync run {self.run_id}: " if self.run_id else "sync: "
        return scope + (
            f"{self.imported.total_rows} scraper rows "
            f"({self.imported.refreshed} refreshed, {self.imported.created} created, "
            f"{self.imported.dups} dups) | dishes: "
            f"{self.dishes.extracted} scanned, {self.dishes.with_dish} with a dish, "
            f"{self.dishes.no_dish} without"
            + (
                f" | menus: {self.menus.items} item(s) into"
                f" {self.menus.leads_updated} lead(s),"
                f" {self.menus.refused_junk} junk refused"
                if self.menus is not None
                else ""
            )
        )


def _clear_annotations(db: "DatabaseType", run_id: str | None) -> int:
    """Drop dish annotations so the miner can re-judge them.

    Needed because a tightened proposer cannot fix rows it is told to skip: the
    first pass stored `korean`, `pub`, `variety`, `made` as dishes, and those
    leads would otherwise keep them forever (idempotence is per-row).
    """
    scope = " WHERE state IN ('QUALIFIED', 'ROUTED')" + (
        " AND run_id=?" if run_id is not None else ""
    )
    params: tuple[Any, ...] = (run_id,) if run_id is not None else ()
    with db.conn:
        cur = db.conn.execute(
            "UPDATE leads SET extracted_dish=NULL, dish_json=NULL" + scope, params
        )
    return int(cur.rowcount or 0)


def sync_evidence(
    db: "DatabaseType",
    scraper_db: str | Path = SCRAPER_DB,
    *,
    run_id: str | None = None,
    remine: bool = False,
) -> SyncReport:
    """Refresh leads from the scraper, then annotate dishes. Idempotent.

    `run_id` confines both stages to one scraper run, so an enrichment pass is
    applied to exactly the leads that run produced.
    """
    if remine:
        _clear_annotations(db, run_id)
    imported = refresh_leads(db, scraper_db, run_id=run_id)
    # Owner order 2026-11-12: the scraper's website-menu pass is first-class
    # dish evidence. It runs AFTER the refresh because refresh rewrites
    # `data_json` from the scraper row, which does not carry the menu pass.
    menus = import_menu_items(db, scraper_db)
    # Deterministic only: the first real backfill died with `OllamaDead` after
    # three refused connections, so this pass must never depend on the model.
    # Leads without a minable dish fall back to the no-dish copy (`no_model`).
    dishes = extract_dishes(db, assert_live=False, use_model=False, run_id=run_id)
    return SyncReport(imported=imported, dishes=dishes, menus=menus, run_id=run_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh outreach leads and mine dishes.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--scraper", default=str(SCRAPER_DB))
    parser.add_argument("--run", default=None, help="one scraper run_id")
    parser.add_argument(
        "--remine", action="store_true",
        help="re-judge existing dish annotations (use after a proposer change)",
    )
    args: Any = parser.parse_args(argv)
    with Database(Path(args.db)) as db:
        report = sync_evidence(
            db, args.scraper, run_id=args.run, remine=args.remine
        )
    print(report.as_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
