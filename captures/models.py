import uuid
from django.db import models


class Capture(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    timestamp = models.DateTimeField(db_index=True)
    device = models.CharField(max_length=50, db_index=True)  # "mac", "iphone"
    app = models.CharField(max_length=255, blank=True, default="")
    window = models.CharField(max_length=500, blank=True, default="")
    url = models.URLField(max_length=2000, blank=True, default="")
    image = models.ImageField(upload_to="captures/%Y-%m-%d/", blank=True)
    has_image = models.BooleanField(default=False)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["device", "timestamp"]),
            models.Index(fields=["app", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.timestamp:%H:%M:%S} — {self.app} ({self.device})"
