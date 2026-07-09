"""Batch analysis of capture blocks using Claude Haiku."""
import json
import logging

import anthropic
from django.conf import settings
from django.db.models import Min, Max, Count
from django.utils import timezone

from .models import Capture, Project, ActivitySummary

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a time-tracking assistant. You receive batches of computer activity blocks \
(app name, window title, URL) and categorise each one.

For each block, return:
- "project": a short project or category name (e.g. "Meditation App", "Resilient Minds", \
"Screenwatch", "Job Search", "YouTube", "Email", "Browsing", etc.). \
Use consistent names — reuse existing project names when the activity clearly belongs to one.
- "description": a concise sentence about what the user was doing \
(e.g. "Building audio generation pipeline in Django", "Watching Kabbalah lecture").

Known projects so far: {known_projects}

Respond with a JSON array, one object per block, in the same order as the input. \
Example: [{"project": "Screenwatch", "description": "Configuring Cloudinary storage in Django settings"}]
Only return the JSON array, nothing else."""


def _build_block_list(captures):
    """Collapse consecutive captures with the same app+window+url into blocks."""
    blocks = []
    current = None
    for c in captures:
        key = (c.app, c.window, c.url)
        if current and current["key"] == key:
            current["end"] = c.timestamp
            current["ticks"] += 1
            current["capture_ids"].append(c.id)
        else:
            if current:
                blocks.append(current)
            current = {
                "key": key,
                "app": c.app,
                "window": c.window,
                "url": c.url,
                "start": c.timestamp,
                "end": c.timestamp,
                "ticks": 1,
                "capture_ids": [c.id],
            }
    if current:
        blocks.append(current)
    return blocks


def analyse_unprocessed():
    """Find captures without an activity summary, group into blocks, and analyse."""
    unprocessed = (
        Capture.objects
        .filter(activity__isnull=True)
        .order_by("timestamp")
    )
    if not unprocessed.exists():
        return 0

    # Don't analyse the very latest block — it might still be growing
    latest_ts = unprocessed.last().timestamp
    cutoff = latest_ts - timezone.timedelta(seconds=30)
    to_process = unprocessed.filter(timestamp__lt=cutoff)
    if not to_process.exists():
        return 0

    blocks = _build_block_list(to_process)
    if not blocks:
        return 0

    # Build the prompt
    known = list(Project.objects.values_list("name", flat=True))
    known_str = ", ".join(known) if known else "(none yet — discover them)"

    block_descriptions = []
    for i, b in enumerate(blocks):
        secs = b["ticks"] * 5
        desc = f"Block {i+1}: app=\"{b['app']}\", window=\"{b['window']}\""
        if b["url"]:
            desc += f", url=\"{b['url']}\""
        desc += f", duration={secs}s"
        block_descriptions.append(desc)

    user_msg = "\n".join(block_descriptions)

    # Call Claude Haiku
    api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set, skipping analysis")
        return 0

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT.format(known_projects=known_str),
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception:
        logger.exception("Claude API call failed")
        return 0

    # Parse response
    text = response.content[0].text.strip()
    try:
        results = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse Claude response: %s", text)
        return 0

    if len(results) != len(blocks):
        logger.error(
            "Result count mismatch: got %d, expected %d", len(results), len(blocks)
        )
        return 0

    # Save results
    created = 0
    for block, result in zip(blocks, results):
        project_name = result.get("project", "Uncategorised")
        description = result.get("description", "")

        project, _ = Project.objects.get_or_create(name=project_name)
        summary = ActivitySummary.objects.create(
            project=project,
            description=description,
            start_time=block["start"],
            end_time=block["end"],
            tick_count=block["ticks"],
        )
        Capture.objects.filter(id__in=block["capture_ids"]).update(activity=summary)
        created += 1

    return created
