import uuid
from django.db import models


class Project(models.Model):
    name = models.CharField(max_length=255, unique=True)
    first_seen = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Capture(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(db_index=True)
    device = models.CharField(max_length=50, db_index=True)  # "mac", "iphone"
    app = models.CharField(max_length=255, blank=True, default="")
    window = models.CharField(max_length=500, blank=True, default="")
    url = models.URLField(max_length=2000, blank=True, default="")
    image = models.ImageField(upload_to="captures/%Y-%m-%d/", blank=True)
    has_image = models.BooleanField(default=False)
    activity = models.ForeignKey(
        "ActivitySummary", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="captures",
    )

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["device", "timestamp"]),
            models.Index(fields=["app", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.timestamp:%H:%M:%S} — {self.app} ({self.device})"


class ActivitySummary(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="activities",
    )
    description = models.CharField(max_length=500)
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(db_index=True)
    tick_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-start_time"]

    def __str__(self):
        proj = self.project.name if self.project else "Uncategorised"
        return f"{proj}: {self.description}"

    @property
    def duration_secs(self):
        return self.tick_count * 5
