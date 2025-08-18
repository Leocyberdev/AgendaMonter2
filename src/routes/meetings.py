from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, abort
from flask_login import login_required, current_user
from src.utils.email_utils import send_meeting_notification
from src.utils.notification_utils import create_meeting_notifications, get_user_notifications, get_unread_count
from src.utils.timezone_utils import (
    make_timezone_aware, 
    is_in_past, 
    BRAZIL_TZ, 
    format_datetime_for_input, 
    parse_datetime_from_input,
    get_brazil_now,
    ensure_timezone_aware
)
from src.models.user import db, Room, User
from src.models.meeting import Meeting
from src.models.notification import Notification
from src.forms import MeetingForm, EditMeetingForm
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo
import json

meetings_bp = Blueprint("meetings", __name__)


def check_room_availability(room_id, start_datetime, end_datetime, exclude_meeting_id=None):
    print(f"🔍 Checando sala {room_id} de {start_datetime} até {end_datetime}")
    query = Meeting.query.filter(
        Meeting.room_id == room_id,
        Meeting.start_datetime < end_datetime,
        Meeting.end_datetime > start_datetime
    )
    if exclude_meeting_id:
        query = query.filter(Meeting.id != exclude_meeting_id)

    conflicts = query.all()
    for conflict in conflicts:
        print(f"⚠️ Conflito com {conflict.title} de {conflict.start_datetime} até {conflict.end_datetime}")

    return len(conflicts) == 0, conflicts


# --- FUNÇÃO CORRIGIDA ---
def create_recurring_meetings(base_meeting):
    if not base_meeting.is_recurring or not base_meeting.recurrence_type:
        return []

    if not base_meeting.recurrence_end:
        print("⚠️ Erro: recurrence_end está vazio para uma reunião recorrente.")
        return []

    created_meetings = []

    # Pegamos data inicial e horários fixos da reunião original
    current_date = base_meeting.start_datetime.date()
    start_hour = base_meeting.start_datetime.hour
    start_minute = base_meeting.start_datetime.minute
    start_second = base_meeting.start_datetime.second
    end_hour = base_meeting.end_datetime.hour
    end_minute = base_meeting.end_datetime.minute
    end_second = base_meeting.end_datetime.second

    try:
        end_date = datetime.combine(
            base_meeting.recurrence_end,
            datetime.min.time(),
            tzinfo=BRAZIL_TZ
        )
    except Exception as e:
        print(f"❌ Erro ao processar data de fim da recorrência: {e}")
        return []

    max_iterations = 100
    iteration_count = 0

    while iteration_count < max_iterations:
        iteration_count += 1

        try:
            if base_meeting.recurrence_type == 'daily':
                current_date += timedelta(days=1)
            elif base_meeting.recurrence_type == 'weekly':
                current_date += timedelta(weeks=1)
            elif base_meeting.recurrence_type == 'monthly':
                current_date += relativedelta(months=1)

            if current_date > end_date.date():
                break

            if base_meeting.recurrence_type == 'daily':
                if current_date.weekday() >= 5:  # pula sábado e domingo
                    continue

            # Agora fixamos os horários da reunião original
            new_start_datetime = datetime(
                current_date.year, current_date.month, current_date.day,
                start_hour, start_minute, start_second,
                tzinfo=BRAZIL_TZ
            )
            new_end_datetime = datetime(
                current_date.year, current_date.month, current_date.day,
                end_hour, end_minute, end_second,
                tzinfo=BRAZIL_TZ
            )

            is_available, _ = check_room_availability(
                base_meeting.room_id, new_start_datetime, new_end_datetime
            )

            if is_available:
                new_meeting = Meeting(
                    title=base_meeting.title,
                    description=base_meeting.description,
                    start_datetime=new_start_datetime,
                    end_datetime=new_end_datetime,
                    participants=base_meeting.participants,
                    room_id=base_meeting.room_id,
                    created_by=base_meeting.created_by,
                    parent_meeting_id=base_meeting.id,
                    is_recurring=False,
                    created_at=get_brazil_now()
                )
                db.session.add(new_meeting)
                created_meetings.append(new_meeting)

        except Exception as e:
            print(f"❌ Erro ao criar reunião recorrente para {current_date}: {e}")
            continue

    print(f"📅 Criando reuniões recorrentes...")
    print(f"✅ Processadas {iteration_count} iterações, criadas {len(created_meetings)} reuniões")
    return created_meetings
# --- FIM DA FUNÇÃO CORRIGIDA ---
