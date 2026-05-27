from django.db import models
from config.models import BaseModel


class SupportChannel(BaseModel):
    channel_id = models.CharField(null=True, blank=True)
    join_link = models.CharField()
    name = models.CharField(null=True, blank=True)
    btn_text = models.CharField(null=True, blank=True)

    class Meta:
        db_table = 'SupportChannels'
    
    def __str__(self):
        return self.join_link