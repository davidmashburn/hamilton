#!/usr/bin/env python3
from __future__ import annotations

"""Regenerate annotated Prefect walkthrough screenshots with calibrated DOM->image overlays.

This script drives the reusable calibrated renderer (`dom_calibrated_annotations.py`) across the
walkthrough's Prefect UI tour screenshots. It stores transient clean/calibration images and fit
metadata under /tmp and writes the final annotated screenshots into the repo.

Usage (from anywhere):
  uv run --no-project --with playwright --with pillow --with numpy \
    python /Users/davmash/code/hamilton/examples/prefect/tools/regenerate_prefect_ui_walkthrough_annotations.py

Optional filters:
  ... regenerate_prefect_ui_walkthrough_annotations.py --only deployments-list-live-annotated --only work-pools-list-annotated
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
TOOLS_DIR = ROOT / "examples/prefect/tools"
OUT_DIR = ROOT / "examples/prefect/imgs/orchestration_demo/prefect_ui_walkthrough/annotated"
TOOL_PATH = TOOLS_DIR / "dom_calibrated_annotations.py"

TMP_ROOT = Path("/tmp/prefect_ui_walkthrough_annotations")
SPEC_DIR = TMP_ROOT / "specs"
META_DIR = TMP_ROOT / "meta"
CLEAN_DIR = TMP_ROOT / "clean"
CAL_DIR = TMP_ROOT / "calibration"
for d in [SPEC_DIR, META_DIR, CLEAN_DIR, CAL_DIR]:
    d.mkdir(parents=True, exist_ok=True)

LEGACY_COORDS = {
    "dashboard-overview-annotated": [
        {
            "text": "Primary navigation",
            "selectors": [
                {"selector": "text=Dashboard", "within": "div.p-context-sidebar.context-sidebar"},
                {"selector": "text=Concurrency", "within": "div.p-context-sidebar.context-sidebar"},
            ],
            "pad_css": [16, 14, 150, 14],
            "label_offset_css": [150, -58],
            "edge_refine": False,
        },
        {"text": "Flow run health overview", "selector": "div.workspace-dashboard-flow-runs-card", "label_offset_css": [180, -58]},
        {"text": "Task success vs failure trend", "selector": "div.cumulative-task-runs-card", "label_offset_css": [140, -58]},
        {"text": "Work pool status snapshot", "selector": "div.dashboard-work-pools-card", "label_offset_css": [110, -58]},
    ],
    "runs-list-annotated": [
        {"text": "Filter by date, state, flow, pool, tags", "selector": "div.flow-runs-filter-group", "label_offset_css": [260, -56]},
        {"text": "Duration/state trend for recent runs", "selector": "div.flow-runs-scatter-plot-plot.runs__scatter-plot", "pad_css": [10, 10, 10, 10], "label_offset_css": [200, -36]},
        {
            "text": "Run list with state, params, task count",
            "selectors": [
                "div.list-header.list-header--sticky.min-h-10",
                {"selector": "div.p-list-item.state-list-item", "nth": 0},
                {"selector": "div.p-list-item.state-list-item", "nth": 1},
                {"selector": "div.p-list-item.state-list-item", "nth": 2},
            ],
            "pad_css": [0, 0, 0, 6],
            "label_offset_css": [220, -40],
            "edge_refine": False,
        },
    ],
    "flow-run-success-detail-annotated": [
        {
            "text": "Run state + elapsed time",
            "selector": "div.page-heading-flow-run__flow-details",
            "pad_css": [0, 0, 0, 0],
            "label_offset_css": [320, -36],
            "edge_refine": False,
        },
        {
            "text": "Task Gantt bars for execution",
            "box_css": [578, 182, 393, 75],
            "label_css": [980, 156],
            "edge_refine": False,
        },
        {"text": "Switch tabs: logs, task runs, params", "selector": "ul.p-tab-navigation[role='tablist']", "label_offset_css": [520, -6], "edge_refine": False},
        {"text": "Logs show retries and transitions", "box_css": [206, 529, 1008, 336], "label_css": [518, 492], "edge_refine": False},
    ],
    "flow-run-failed-detail-annotated": [
        {
            "text": "Failed run state is explicit",
            "selector": "div.page-heading-flow-run__flow-details",
            "pad_css": [0, 0, 0, 0],
            "label_offset_css": [320, -36],
            "edge_refine": False,
        },
        {
            "text": "Red timeline section marks failure window",
            "box_css": [730, 85, 486, 295],
            "label_css": [894, 52],
            "edge_refine": False,
        },
        {"text": "Failed task highlighted in red", "box_css": [679, 227, 301, 58], "label_css": [980, 230], "edge_refine": False},
        {"text": "Failure logs + retry exhaustion details", "box_css": [206, 529, 1008, 335], "label_css": [472, 492], "edge_refine": False},
    ],
    "flows-catalog-annotated": [
        {"text": "Search + tag + sort controls", "selector": "div.list-header.list-header--sticky", "label_offset_css": [620, -50], "edge_refine": False},
        {"text": "Flow catalog and most recent status", "selector": "div.p-table.flow-list__table", "label_offset_css": [260, -46]},
        {"text": "Deployment linkage per flow", "selector": "a.deployments-count.flow-list__deployment-count", "pad_css": [16, 16, 16, 16], "label_offset_css": [120, -48], "edge_refine": False},
    ],
    "work-pools-list-annotated": [
        {
            "text": "Search/filter work pools",
            "selector": "input[placeholder*='Search work pools']",
            "pad_css": [8, 6, 8, 6],
            "label_offset_css": [-170, -46],
            "edge_refine": False,
        },
        {
            "text": "Work pool list + health snapshot",
            "selectors": [
                "div.work-pool-card__header",
                "div.work-pool-card__details",
            ],
            "pad_css": [12, 12, 12, 12],
            "label_offset_css": [260, -58],
            "edge_refine": False,
        },
    ],
    "blocks-empty-state-annotated": [
        {
            "text": "Add block button",
            "selector": "text=Add Block",
            "pad_css": [8, 6, 8, 6],
            "label_offset_css": [-280, -56],
            "edge_refine": False,
        },
        {"text": "Blocks store credentials/config", "selector": "div.blocks-page-empty-state", "label_offset_css": [260, -46]},
    ],
    "variables-empty-state-annotated": [
        {
            "text": "Add variable button",
            "selector": "text=Add Variable",
            "pad_css": [8, 6, 8, 6],
            "label_offset_css": [-270, -56],
            "edge_refine": False,
        },
        {"text": "Variables for non-secret runtime values", "selector": "div.p-card.p-card.p-background.p-empty-state", "label_offset_css": [220, -46]},
    ],
    "automations-empty-state-annotated": [
        {
            "text": "Add automation button",
            "selector": "text=Add Automation",
            "pad_css": [8, 6, 8, 6],
            "label_offset_css": [-300, -56],
            "edge_refine": False,
        },
        {"text": "Event-driven triggers and actions", "selector": "div.automations-page-empty-state", "label_offset_css": [250, -46]},
    ],
    "event-feed-annotated": [
        {"text": "Filter by resource and event type", "selector": "div.events__filters", "label_offset_css": [300, -42], "edge_refine": False},
        {"text": "Event volume/time graph", "selector": "div.events__chart", "label_offset_css": [360, 4]},
        {
            "text": "Structured event timeline (retries, failures)",
            "selector": "div.p-card.p-card.p-background.workspace-events-timeline-content",
            "pad_css": [130, 24, 0, 290],
            "label_offset_css": [180, -44],
            "edge_refine": False,
        },
    ],
    "concurrency-limits-annotated": [
        {
            "text": "Global vs task-run limits",
            "selectors": [
                {"selector": "ul.p-tab-navigation li", "nth": 0},
                {"selector": "ul.p-tab-navigation li", "nth": 1},
            ],
            "pad_css": [0, 0, 0, 0],
            "label_offset_css": [230, -44],
            "edge_refine": False,
        },
        {"text": "Create limit", "selector": "button.p-button--outline", "nth": 0, "label_offset_css": [34, -8], "edge_refine": False},
        {"text": "Concurrency controls to protect systems", "selector": "div.concurrency-limit-page-empty-state", "label_offset_css": [260, -46]},
    ],
}

COMMON = {
    "viewport": {"width": 1280, "height": 800},
    "device_scale_factor": 1.0,
    "screenshot_scale": "css",
    "full_page": True,
    "wait_for_ms": 1200,
    "close_prefect_modal": True,
    "max_fit_error_px": 1.25,
    "chromium_executable_path": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "screenshot_style_css": """
div[role='dialog'][aria-modal='true'],
div[aria-label='Join the Prefect Community'],
.Toastify,
[data-testid='toast'],
[aria-live='polite'],
[aria-live='assertive'] {
  visibility: hidden !important;
  opacity: 0 !important;
  pointer-events: none !important;
}
""",
}


def _load_renderer_module():
    spec = importlib.util.spec_from_file_location("dom_cal_annot", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load renderer module from {TOOL_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def build_shots(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "dashboard-overview-annotated",
            "url": f"{base_url}/",
            "wait_for_selector": "text=Flow Runs",
            "callouts": LEGACY_COORDS["dashboard-overview-annotated"],
        },
        {
            "name": "runs-list-annotated",
            "url": f"{base_url}/runs",
            "wait_for_selector": "text=Flow runs",
            "callouts": LEGACY_COORDS["runs-list-annotated"],
        },
        {
            "name": "flow-run-success-detail-annotated",
            "url": f"{base_url}/runs",
            "wait_for_selector": "text=Flow runs",
            "actions": [
                {"type": "click", "selector": "text=outrageous-wolverine", "timeout_ms": 8000},
                {"type": "wait", "ms": 1200},
                {"type": "click", "selector": "text=Logs", "timeout_ms": 5000},
                {"type": "wait", "ms": 400},
            ],
            "callouts": LEGACY_COORDS["flow-run-success-detail-annotated"],
        },
        {
            "name": "flow-run-failed-detail-annotated",
            "url": f"{base_url}/runs",
            "wait_for_selector": "text=Flow runs",
            "actions": [
                {"type": "click", "selector": "text=sticky-ibex", "timeout_ms": 8000},
                {"type": "wait", "ms": 1200},
                {"type": "click", "selector": "text=Logs", "timeout_ms": 5000},
                {"type": "wait", "ms": 400},
            ],
            "callouts": LEGACY_COORDS["flow-run-failed-detail-annotated"],
        },
        {
            "name": "flows-catalog-annotated",
            "url": f"{base_url}/flows",
            "wait_for_selector": "text=Flows",
            "callouts": LEGACY_COORDS["flows-catalog-annotated"],
        },
        {
            "name": "deployments-list-live-annotated",
            "url": f"{base_url}/deployments",
            "wait_for_selector": "text=absenteeism-local",
            "callouts": [
                {
                    "text": "Deployment row + current status",
                    "selector": "div.p-table.deployment-list__table tbody tr.p-table-row",
                    "pad_css": [0, 0, 0, 0],
                    "label_offset_css": [160, -42],
                    "edge_refine": False,
                },
                {
                    "text": "Activity sparkline for run history",
                    "selector": "div.p-table.deployment-list__table tbody td.deployment-list__activity-column",
                    "pad_css": [8, 14, 8, 14],
                    "label_offset_css": [80, -52],
                    "edge_refine": False,
                },
                {
                    "text": "Hourly schedule attached",
                    "selector": "div.deployment-schedule-tags",
                    "pad_css": [8, 8, 8, 8],
                    "label_offset_css": [-20, -42],
                    "edge_refine": False,
                },
            ],
        },
        {
            "name": "work-pools-list-annotated",
            "url": f"{base_url}/work-pools",
            "wait_for_selector": "text=Work Pools",
            "callouts": LEGACY_COORDS["work-pools-list-annotated"],
        },
        {
            "name": "blocks-empty-state-annotated",
            "url": f"{base_url}/blocks",
            "wait_for_selector": "text=Blocks",
            "callouts": LEGACY_COORDS["blocks-empty-state-annotated"],
        },
        {
            "name": "variables-empty-state-annotated",
            "url": f"{base_url}/variables",
            "wait_for_selector": "text=Variables",
            "callouts": LEGACY_COORDS["variables-empty-state-annotated"],
        },
        {
            "name": "automations-empty-state-annotated",
            "url": f"{base_url}/automations",
            "wait_for_selector": "text=Automations",
            "callouts": LEGACY_COORDS["automations-empty-state-annotated"],
        },
        {
            "name": "event-feed-annotated",
            "url": f"{base_url}/events",
            "wait_for_selector": "text=Event Feed",
            "callouts": LEGACY_COORDS["event-feed-annotated"],
        },
        {
            "name": "concurrency-limits-annotated",
            "url": f"{base_url}/concurrency-limits",
            "wait_for_selector": "text=Add a concurrency limit",
            "callouts": LEGACY_COORDS["concurrency-limits-annotated"],
        },
    ]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:4200")
    ap.add_argument(
        "--mode",
        choices=["dom-overlay", "legacy-calibrated"],
        default="dom-overlay",
        help="Annotation renderer mode. dom-overlay is preferred; legacy-calibrated is fallback.",
    )
    ap.add_argument("--only", action="append", default=[], help="Repeatable screenshot name filter")
    ap.add_argument("--list", action="store_true", help="List available screenshot names and exit")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    renderer = _load_renderer_module()
    shots = build_shots(args.base_url.rstrip("/"))
    if args.list:
        for shot in shots:
            print(shot["name"])
        return 0

    allow = set(args.only or [])
    summary: list[dict[str, Any]] = []
    for shot in shots:
        if allow and shot["name"] not in allow:
            continue
        spec = dict(COMMON)
        spec.update({k: v for k, v in shot.items() if k not in {"name"}})
        spec_path = SPEC_DIR / f"{shot['name']}.json"
        clean_path = CLEAN_DIR / f"{shot['name']}-clean.png"
        cal_path = CAL_DIR / f"{shot['name']}-cal.png"
        meta_path = META_DIR / f"{shot['name']}.json"
        out_path = OUT_DIR / f"{shot['name']}.png"
        spec_path.write_text(json.dumps(spec, indent=2))
        if args.mode == "dom-overlay":
            argv = [
                "capture-dom-overlay",
                "--spec", str(spec_path),
                "--annotated-out", str(out_path),
                "--metadata-out", str(meta_path),
                "--clean-screenshot", str(clean_path),
            ]
        else:
            argv = [
                "capture-and-render",
                "--spec", str(spec_path),
                "--clean-screenshot", str(clean_path),
                "--annotated-out", str(out_path),
                "--metadata-out", str(meta_path),
                "--calibration-screenshot", str(cal_path),
            ]
        print(f"\n=== {shot['name']} ===")
        rc = int(renderer.main(argv))
        if rc != 0:
            return rc
        meta = json.loads(meta_path.read_text())
        if "fit" in meta:
            fit = meta["fit"]
            summary.append({"name": shot["name"], "mode": args.mode, "max_error_px": fit["max_error_px"], "rms_error_px": fit["rms_error_px"]})
        else:
            summary.append({"name": shot["name"], "mode": args.mode})

    print("\n=== SUMMARY ===")
    for row in summary:
        if "max_error_px" in row:
            print(f"{row['name']} [{row['mode']}]: max={row['max_error_px']:.3f}px rms={row['rms_error_px']:.3f}px")
        else:
            print(f"{row['name']} [{row['mode']}]")
    (META_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Metadata dir: {META_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
