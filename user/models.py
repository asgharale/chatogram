from django.db import models
from config.models import BaseModel, Document, City, Province
from .enums import GENDER


class UserProfile(BaseModel):
    bale_id = models.PositiveBigIntegerField(unique=True)
    username = models.CharField(max_length=150, blank=True, null=True)
    age = models.PositiveIntegerField(blank=True, null=True)
    gender = models.PositiveSmallIntegerField(choices=GENDER, default=3, blank=True, null=True)
    province = models.ForeignKey(Province, on_delete=models.SET_NULL, null=True)
    city = models.ForeignKey(City, on_delete=models.SET_NULL, null=True)
    profile_picture = models.ForeignKey(Document, on_delete=models.SET_NULL, null=True, blank=True)
    first_name = models.CharField(max_length=50, blank=True, null=True)
    last_name = models.CharField(max_length=50, blank=True, null=True)
    full_name = models.CharField(max_length=60, blank=True, null=True)
    national_code = models.CharField(max_length=14, blank=True, null=True)
    phone = models.CharField(max_length=15, blank=True, null=True)


    class Meta:
        db_table = 'UserProfiles'
        indexes = [
            models.Index(fields=['city']),
            models.Index(fields=['age']),
        ]

    def __str__(self):
        return f"{self.bale_id} - {self.username}"