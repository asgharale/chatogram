from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.cache import cache
import logging

from .serializers import BaleBotWebhookSerializer
from .tasks import process_webhook_task

logger = logging.getLogger(__name__)


class BaleBotWebhook(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        serializer = BaleBotWebhookSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(
                "Bad webhook payload | errors=%s | raw=%s",
                serializer.errors, request.data,
            )
            return Response({"ok": True})   # Always 200 — never let Bale retry

        data     = serializer.validated_data
        message  = data.get("message")
        callback = data.get("callback_query")

        if not message and not callback:
            return Response({"ok": True})

        # ── 2. Deduplication — Bale retries the same update if we're slow ─────
        dedup_id = None
        if message and message.get("message_id"):
            dedup_id = f"msg_{message['message_id']}"
        elif callback and callback.get("id"):
            dedup_id = f"cb_{callback['id']}"

        if dedup_id:
            if not cache.add(f"wh_seen:{dedup_id}", 1, timeout=300):
                logger.debug("Duplicate webhook skipped: %s", dedup_id)
                return Response({"ok": True})

        # ── 3. Off-load everything — back in < 50 ms ──────────────────────────
        process_webhook_task.delay(request.data)
        return Response({"ok": True})