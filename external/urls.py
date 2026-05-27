from external.views import BaleBotWebhook
from django.urls import path


urlpatterns = [
    path("webhook/", BaleBotWebhook.as_view(), name='web-hook'),
    # path("test-send-message/", SendMessage.as_view())
]