from django.db import models
from config.models import BaseModel
from .enums import MESSAGE_TYPE, CHAT_STATUS
from user.models import UserProfile



class ChatSession(BaseModel):
    user1 = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, related_name='chats_as_user1')
    user2 = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, related_name='chats_as_user2')
    status = models.PositiveSmallIntegerField(choices=CHAT_STATUS, default=0)
    end_date = models.DateTimeField(null=True, blank=True) 


    class Meta:
        db_table = 'ChatSessions'


class Message(BaseModel):
    sender = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, related_name='sent_messages')
    receiver = models.ForeignKey(UserProfile, on_delete=models.SET_NULL, null=True, related_name='received_messages')
    chat = models.ForeignKey(ChatSession, on_delete=models.SET_NULL, null=True)

    static_id = models.PositiveIntegerField(blank=True, null=True)
    sender_chat_id = models.PositiveIntegerField(blank=True, null=True)
    message_id = models.PositiveIntegerField()
    user_chat_id = models.PositiveIntegerField()
    text = models.TextField(null=True, blank=True)
    type = models.PositiveSmallIntegerField(choices=MESSAGE_TYPE)
    is_success = models.BooleanField(default=True)

    class Meta:
        db_table = 'Messages'
        unique_together = [['chat', 'message_id']]