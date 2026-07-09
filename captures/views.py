import json
import logging
import threading
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Sum, F, Value, CharField
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from .models import Capture, ActivitySummary, Project

logger = logging.getLogger(__name__)

_analysis_counter = 0
_ANALYSIS_EVERY = 20  # run analysis every N captures


def _check_api_key(request):
    key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not key or key != settings.SCREENWATCH_API_KEY:
        return JsonResponse({"error": "unauthorized"}, status=401)
    return None


@csrf_exempt
@require_POST
def api_capture(request):
    """Accept a screenshot + metadata from any device."""
    auth_err = _check_api_key(request)
    if auth_err:
        return auth_err

    ts = request.POST.get("timestamp") or timezone.now().isoformat()
    device = request.POST.get("device", "unknown")
    app = request.POST.get("app", "")
    window = request.POST.get("window", "")
    url = request.POST.get("url", "")
    image = request.FILES.get("image")

    from django.utils.dateparse import parse_datetime
    parsed_ts = parse_datetime(ts)
    if not parsed_ts:
        parsed_ts = timezone.now()
    if timezone.is_naive(parsed_ts):
        from django.utils.timezone import make_aware
        parsed_ts = make_aware(parsed_ts)

    capture = Capture(
        timestamp=parsed_ts,
        device=device,
        app=app,
        window=window,
        url=url,
        has_image=bool(image),
    )
    if image:
        capture.image = image
    capture.save()

    # Trigger analysis every N captures (in background thread to avoid blocking)
    global _analysis_counter
    _analysis_counter += 1
    if _analysis_counter >= _ANALYSIS_EVERY:
        _analysis_counter = 0
        threading.Thread(target=_run_analysis, daemon=True).start()

    return JsonResponse({"ok": True, "id": str(capture.id)}, status=201)


def _run_analysis():
    try:
        from .analysis import analyse_unprocessed
        count = analyse_unprocessed()
        if count:
            logger.info("Analysed %d activity blocks", count)
    except Exception:
        logger.exception("Background analysis failed")


@csrf_exempt
@require_POST
def api_capture_batch(request):
    """Accept multiple metadata-only entries in one request (for the Mac script)."""
    auth_err = _check_api_key(request)
    if auth_err:
        return auth_err

    try:
        entries = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)

    from django.utils.dateparse import parse_datetime
    from django.utils.timezone import make_aware

    created = 0
    for entry in entries:
        parsed_ts = parse_datetime(entry.get("timestamp", ""))
        if not parsed_ts:
            parsed_ts = timezone.now()
        if timezone.is_naive(parsed_ts):
            parsed_ts = make_aware(parsed_ts)

        Capture.objects.create(
            timestamp=parsed_ts,
            device=entry.get("device", "unknown"),
            app=entry.get("app", ""),
            window=entry.get("window", ""),
            url=entry.get("url", ""),
            has_image=False,
        )
        created += 1

    return JsonResponse({"ok": True, "created": created}, status=201)


@require_GET
def dashboard(request):
    """Main dashboard — timeline + app totals for a given day."""
    date_str = request.GET.get("date")
    if date_str:
        from django.utils.dateparse import parse_date
        day = parse_date(date_str)
    else:
        day = timezone.localdate()

    day_start = timezone.make_aware(
        timezone.datetime.combine(day, timezone.datetime.min.time())
    )
    day_end = day_start + timedelta(days=1)

    captures = Capture.objects.filter(
        timestamp__gte=day_start, timestamp__lt=day_end
    ).order_by("timestamp")

    # Build activity blocks
    blocks = []
    current = None
    for c in captures:
        key = (c.app, c.window, c.url, c.device)
        if current and current["key"] == key:
            current["end"] = c.timestamp
            current["ticks"] += 1
            if c.has_image:
                current["last_image"] = c.image.url if c.image else None
        else:
            if current:
                blocks.append(current)
            current = {
                "key": key,
                "app": c.app,
                "window": c.window,
                "url": c.url,
                "device": c.device,
                "start": c.timestamp,
                "end": c.timestamp,
                "ticks": 1,
                "last_image": c.image.url if c.has_image and c.image else None,
            }
    if current:
        blocks.append(current)

    # Calculate durations
    for b in blocks:
        secs = b["ticks"] * 5
        b["duration_mins"] = secs // 60
        b["duration_secs"] = secs % 60
        b["duration_display"] = (
            f"{b['duration_mins']}m {b['duration_secs']}s"
            if b["duration_mins"]
            else f"{b['duration_secs']}s"
        )

    # Per-app totals
    app_totals = {}
    for b in blocks:
        label = f"{b['app']} ({b['device']})" if b["app"] else f"({b['device']})"
        app_totals[label] = app_totals.get(label, 0) + b["ticks"] * 5
    app_totals_sorted = sorted(app_totals.items(), key=lambda x: -x[1])
    max_secs = app_totals_sorted[0][1] if app_totals_sorted else 1
    app_totals_display = [
        {
            "app": app,
            "secs": secs,
            "pct": max(int(secs / max_secs * 100), 3),
            "display": f"{secs // 3600}h {(secs % 3600) // 60}m" if secs >= 3600 else f"{secs // 60}m {secs % 60}s",
        }
        for app, secs in app_totals_sorted
    ]

    total_secs = sum(s for _, s in app_totals.items())
    prev_day = (day - timedelta(days=1)).isoformat()
    next_day = (day + timedelta(days=1)).isoformat()

    # Project breakdowns from analysed activity summaries
    activities = (
        ActivitySummary.objects
        .filter(start_time__gte=day_start, start_time__lt=day_end)
        .select_related("project")
        .order_by("start_time")
    )

    project_totals = {}
    for a in activities:
        name = a.project.name if a.project else "Uncategorised"
        if name not in project_totals:
            project_totals[name] = {"active": 0, "waiting": 0}
        project_totals[name][a.status] += a.duration_secs
    project_totals_sorted = sorted(
        project_totals.items(), key=lambda x: -(x[1]["active"] + x[1]["waiting"])
    )
    project_max = (
        (project_totals_sorted[0][1]["active"] + project_totals_sorted[0][1]["waiting"])
        if project_totals_sorted else 1
    )

    def _fmt(secs):
        if secs >= 3600:
            return f"{secs // 3600}h {(secs % 3600) // 60}m"
        return f"{secs // 60}m {secs % 60}s"

    project_totals_display = []
    for name, times in project_totals_sorted:
        total = times["active"] + times["waiting"]
        active_pct = int(times["active"] / total * 100) if total else 100
        display = _fmt(total)
        if times["waiting"]:
            display += f" ({_fmt(times['active'])} active, {_fmt(times['waiting'])} waiting)"
        project_totals_display.append({
            "project": name,
            "secs": total,
            "active_secs": times["active"],
            "waiting_secs": times["waiting"],
            "pct": max(int(total / project_max * 100), 3),
            "active_pct": active_pct,
            "display": display,
        })

    # Activity timeline (analysed blocks with descriptions)
    activity_timeline = [
        {
            "project": a.project.name if a.project else "Uncategorised",
            "description": a.description,
            "status": a.status,
            "start": a.start_time,
            "end": a.end_time,
            "duration_display": (
                f"{a.duration_secs // 60}m {a.duration_secs % 60}s"
                if a.duration_secs >= 60
                else f"{a.duration_secs}s"
            ),
        }
        for a in activities
    ]

    return render(request, "captures/dashboard.html", {
        "day": day,
        "blocks": blocks,
        "app_totals": app_totals_display,
        "project_totals": project_totals_display,
        "activity_timeline": activity_timeline,
        "total_secs": total_secs,
        "total_display": f"{total_secs // 3600}h {(total_secs % 3600) // 60}m" if total_secs >= 3600 else f"{total_secs // 60}m",
        "capture_count": captures.count(),
        "image_count": captures.filter(has_image=True).count(),
        "prev_day": prev_day,
        "next_day": next_day,
        "devices": list(captures.values_list("device", flat=True).distinct().order_by("device")),
    })
