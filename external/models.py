from django.db import models
from config.models import BaseModel


class SupportChannel(BaseModel):
    channel_id = models.CharField(max_length=100, null=True, blank=True)
    join_link  = models.CharField(max_length=255)
    name       = models.CharField(max_length=100, null=True, blank=True)
    btn_text   = models.CharField(max_length=100, null=True, blank=True)

    class Meta:
        db_table = 'SupportChannels'

    def __str__(self):
        return self.name or self.join_link