from __future__ import absolute_import, unicode_literals

import os

from celery import Celery

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "chatogram.settings"
)

app = Celery("chatogram")

app.config_from_object(
    "django.conf:settings",
    namespace="CELERY"
)

app.autodiscover_tasks()

# import os
# from celery import Celery
# from celery.schedules import crontab

# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'chatogram.settings')

# app = Celery('chatogram')

# app.config_from_object('django.conf:settings', namespace='CELERY')

# app.autodiscover_tasks()

# app.conf.update(
#     worker_pool='threads',
#     worker_prefetch_multiplier=1,
#     worker_max_tasks_per_child=1000,
# )

# @app.task(bind=True)
# def debug_task(self):
#     print(f'Request: {self.request!r}')
