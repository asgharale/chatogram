from rest_framework import serializers


class UserSerializer(serializers.Serializer):
    id = serializers.BigIntegerField(required=False, allow_null=True)
    is_bot = serializers.BooleanField(required=False, allow_null=True)
    first_name = serializers.CharField(required=False, allow_null=True)
    last_name = serializers.CharField(required=False, allow_null=True)
    username = serializers.CharField(required=False, allow_null=True)
    language_code = serializers.CharField(required=False, allow_null=True)


class ChatSerializer(serializers.Serializer):
    id = serializers.BigIntegerField(required=False, allow_null=True)
    first_name = serializers.CharField(required=False, allow_null=True)
    last_name = serializers.CharField(required=False, allow_null=True)
    username = serializers.CharField(required=False, allow_null=True)


class ContactSerializer(serializers.Serializer):
    phone_number = serializers.CharField(required=False, allow_null=True)
    user_id = serializers.BigIntegerField(required=False, allow_null=True)


class PhotoSizeSerializer(serializers.Serializer):
    file_id = serializers.CharField(required=False, allow_null=True)

    file_unique_id = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        default=""
    )

    width = serializers.IntegerField(required=False, allow_null=True)
    height = serializers.IntegerField(required=False, allow_null=True)
    file_size = serializers.IntegerField(required=False, allow_null=True)


class DocumentSerializer(serializers.Serializer):
    file_id = serializers.CharField(required=False, allow_null=True)

    file_unique_id = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        default=""
    )

    file_name = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True
    )

    mime_type = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True
    )

    file_size = serializers.IntegerField(required=False, allow_null=True)


class MessageSerializer(serializers.Serializer):
    message_id = serializers.BigIntegerField(required=False, allow_null=True)

    text = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True
    )

    contact = ContactSerializer(required=False, allow_null=True)
    chat = ChatSerializer(required=False, allow_null=True)
    from_user = UserSerializer(source="from", required=False)

    document = DocumentSerializer(required=False, allow_null=True)

    photo = PhotoSizeSerializer(
        many=True,
        required=False,
        allow_null=True
    )

class CallbackQuerySerializer(serializers.Serializer):
    id      = serializers.BigIntegerField(required=False, allow_null=True)
    data    = serializers.CharField(required=False, allow_null=True)
    message = MessageSerializer(required=False, allow_null=True)


class BaleBotWebhookSerializer(serializers.Serializer):
    message        = MessageSerializer(required=False, allow_null=True)
    callback_query = CallbackQuerySerializer(required=False, allow_null=True)