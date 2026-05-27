from django.db import models


class ActiveQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)
    def inactive(self):
        return self.filter(is_active=False)

class ActiveManager(models.Manager):
    def get_queryset(self):
        return ActiveQuerySet(self.model, using=self._db).filter(is_active=True)

class BaseModel(models.Model):
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ActiveManager()
    all_objects = ActiveQuerySet.as_manager()

    class Meta:
        abstract = True

    def     soft_delete(self):
        self.is_active = False
        self.save()


class Province(BaseModel):
    name = models.CharField(max_length=25, unique=True)

    def __str__(self):
        return self.name
    
    class Meta:
        db_table = 'Provinces'


class City(BaseModel):
    Province = models.ForeignKey(Province, on_delete=models.SET_NULL, null=True)
    name = models.CharField(max_length=25, unique=True)

    class Meta:
        db_table = 'Cities'

    def __str__(self):
        return self.name


class Document(BaseModel):
    Image = models.ImageField(upload_to='Documents/%Y-%M-%d')

    class Meta:
        db_table = 'Documents'