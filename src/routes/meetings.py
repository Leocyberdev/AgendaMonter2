
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
import pytz
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


def create_recurring_meetings(base_meeting, fixed_start_time, fixed_end_time):
    if not base_meeting.is_recurring or not base_meeting.recurrence_type:
        return []

    if not base_meeting.recurrence_end:
        print("⚠️ Erro: recurrence_end está vazio para uma reunião recorrente.")
        return []

    created_meetings = []

    brazil_tz = pytz.timezone('America/Sao_Paulo')
    
    if base_meeting.start_datetime.tzinfo is None:
        base_start = brazil_tz.localize(base_meeting.start_datetime)
    else:
        base_start = base_meeting.start_datetime.astimezone(brazil_tz)
    
    current_date = base_start.date()
    start_hour, start_minute, start_second = fixed_start_time.hour, fixed_start_time.minute, fixed_start_time.second
    end_hour, end_minute, end_second = fixed_end_time.hour, fixed_end_time.minute, fixed_end_time.second

    try:
        if isinstance(base_meeting.recurrence_end, datetime):
            if base_meeting.recurrence_end.tzinfo is None:
                end_date = brazil_tz.localize(base_meeting.recurrence_end)
            else:
                end_date = base_meeting.recurrence_end.astimezone(brazil_tz)
        else:
            end_date = brazil_tz.localize(datetime.combine(
                base_meeting.recurrence_end,
                datetime.min.time()
            ))
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

            new_start_datetime = brazil_tz.localize(datetime(
                current_date.year, current_date.month, current_date.day,
                start_hour, start_minute, start_second
            ))
            new_end_datetime = brazil_tz.localize(datetime(
                current_date.year, current_date.month, current_date.day,
                end_hour, end_minute, end_second
            ))

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


def format_datetime_brazil(dt):
    """Função auxiliar para formatar datetime com timezone do Brasil"""
    if dt is None:
        return ""
    
    brazil_tz = pytz.timezone('America/Sao_Paulo')
    
    if dt.tzinfo is None:
        dt = brazil_tz.localize(dt)
    else:
        dt = dt.astimezone(brazil_tz)
    
    return dt


@meetings_bp.route('/dashboard')
@login_required
def dashboard():
    now_brazil = get_brazil_now()

    upcoming_meetings = Meeting.query.filter(
        Meeting.start_datetime >= now_brazil,
        (Meeting.created_by == current_user.id) | (Meeting.participants.like(f'%{current_user.username}%'))
    ).order_by(Meeting.start_datetime).limit(5).all()

    today = now_brazil.date()
    today_meetings = Meeting.query.filter(
        db.func.date(Meeting.start_datetime) == today,
        (Meeting.created_by == current_user.id) | (Meeting.participants.like(f'%{current_user.username}%'))
    ).order_by(Meeting.start_datetime).all()

    notifications = get_user_notifications(current_user.id, unread_only=False, limit=5)
    unread_count = get_unread_count(current_user.id)

    return render_template('meetings/dashboard.html',
                           upcoming_meetings=upcoming_meetings,
                           today_meetings=today_meetings,
                           notifications=notifications,
                           unread_count=unread_count)


@meetings_bp.route("/create", methods=["GET", "POST"])
@login_required
def create_meeting():
    user = current_user
    form = MeetingForm()

    if form.validate_on_submit():
        original_start = form.start_datetime.data
        original_end = form.end_datetime.data

        start_time = make_timezone_aware(original_start, BRAZIL_TZ)
        end_time = make_timezone_aware(original_end, BRAZIL_TZ)
        room_id = form.room_id.data

        if is_in_past(start_time):
            flash("A data e hora de início não pode ser no passado.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        if end_time <= start_time:
            flash("A data e hora de término deve ser depois da data e hora de início.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        existing_meetings = Meeting.query.filter(
            Meeting.room_id == room_id,
            Meeting.start_datetime < end_time,
            Meeting.end_datetime > start_time
        ).all()

        if existing_meetings:
            flash("A sala selecionada não está disponível neste horário.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        participant_ids = form.participants.data
        is_users_available, conflicting_users = check_user_availability(participant_ids, start_time, end_time)

        if not is_users_available:
            flash(f"Os seguintes usuários já possuem reuniões agendadas neste horário: {', '.join(conflicting_users)}.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        new_meeting = Meeting(
            title=form.title.data,
            description=form.description.data,
            room_id=form.room_id.data,
            start_datetime=start_time,
            end_datetime=end_time,
            is_recurring=form.is_recurring.data,
            recurrence_type=form.recurrence_type.data if form.is_recurring.data else None,
            recurrence_end=form.recurrence_end.data if form.is_recurring.data else None,
            created_by=current_user.id,
            created_at=get_brazil_now()
        )

        participant_names = []
        for participant_id in form.participants.data:
            participant = User.query.get(participant_id)
            if participant:
                participant_names.append(participant.username)

        new_meeting.participants = ", ".join(participant_names) if participant_names else None

        db.session.add(new_meeting)
        db.session.commit()

        all_meetings = [new_meeting]

        if new_meeting.is_recurring:
            try:
                recurring_meetings = create_recurring_meetings(new_meeting, original_start.time(), original_end.time())
                if recurring_meetings:
                    db.session.commit()
                    all_meetings.extend(recurring_meetings)
                    print(f"✅ Criadas {len(recurring_meetings)} reuniões recorrentes")
            except Exception as e:
                print(f"❌ Erro ao criar reuniões recorrentes: {e}")
                db.session.rollback()
                db.session.commit()

        if participant_names:
            participant_emails = []
            for participant_id in form.participants.data:
                participant = User.query.get(participant_id)
                if participant and participant.email:
                    participant_emails.append(participant.email)

            if participant_emails:
                try:
                    start_dt_brazil = format_datetime_brazil(new_meeting.start_datetime)
                    end_dt_brazil = format_datetime_brazil(new_meeting.end_datetime)
                    
                    if new_meeting.is_recurring:
                        recurrence_end_brazil = format_datetime_brazil(
                            datetime.combine(new_meeting.recurrence_end, datetime.min.time()) 
                            if isinstance(new_meeting.recurrence_end, type(datetime.now().date())) 
                            else new_meeting.recurrence_end
                        )
                        subject_suffix = f" (Recorrente até {recurrence_end_brazil.strftime('%d/%m/%Y')})"
                        body_suffix = f"Esta é uma reunião recorrente que se repete {new_meeting.recurrence_type} até {recurrence_end_brazil.strftime('%d/%m/%Y')}."
                    else:
                        subject_suffix = ""
                        body_suffix = ""

                    message_body = f"""
Uma nova reunião foi agendada:

Título: {new_meeting.title}{subject_suffix}
Data: {start_dt_brazil.strftime('%d/%m/%Y')}
Horário: {start_dt_brazil.strftime('%H:%M')} - {end_dt_brazil.strftime('%H:%M')}
Local: {new_meeting.room.name}
Organizador: {new_meeting.creator.username}

{f'Descrição: {new_meeting.description}' if new_meeting.description else ''}
{body_suffix}

Sistema de Reuniões - Monter Elétrica
                    """.strip()

                    send_meeting_notification(
                        new_meeting, 
                        action='created', 
                        recipients=participant_emails + [new_meeting.creator.email],
                        custom_message=message_body
                    )
                    create_meeting_notifications(new_meeting, 'created', participants_only=True)
                    print(f"✅ E-mail enviado para a reunião principal e notificações criadas.")
                except Exception as e:
                    print(f"❌ Erro ao enviar e-mails ou criar notificações: {e}")

        flash("Reunião agendada com sucesso!", "success")
        return redirect(url_for("meetings.dashboard"))

    users = User.query.all()
    rooms = Room.query.all()
    return render_template('meetings/create.html', user=user, rooms=rooms, form=form)


@meetings_bp.route('/my_meetings')
@login_required
def my_meetings():
    my_meetings = Meeting.query.filter_by(
        created_by=current_user.id
    ).order_by(Meeting.created_at.desc(), Meeting.id.desc()).all()

    current_time = get_brazil_now()

    for meeting in my_meetings:
        meeting.start_datetime = ensure_timezone_aware(meeting.start_datetime)
        meeting.end_datetime = ensure_timezone_aware(meeting.end_datetime)
        if meeting.created_at:
            meeting.created_at = ensure_timezone_aware(meeting.created_at)

    return render_template(
        'meetings/my_meetings.html',
        meetings=my_meetings,
        current_time=current_time
    )


@meetings_bp.route('/calendar')
@login_required
def calendar():
    meetings = Meeting.query.order_by(Meeting.start_datetime).all()
    calendar_events = [{
        'id': m.id,
        'title': m.title,
        'start': m.start_datetime.isoformat(),
        'end': m.end_datetime.isoformat(),
        'room': m.room.name,
        'creator': m.creator.username
    } for m in meetings]

    return render_template('meetings/calendar.html', events=json.dumps(calendar_events))


@meetings_bp.route('/edit/<int:meeting_id>', methods=['GET', 'POST'])
@login_required
def edit_meeting(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if meeting.created_by != current_user.id and not current_user.is_admin:
        flash('Você não tem permissão para editar esta reunião.', 'error')
        return redirect(url_for('meetings.my_meetings'))

    form = EditMeetingForm(obj=meeting)
    if form.validate_on_submit():
        start_time = make_timezone_aware(form.start_datetime.data, BRAZIL_TZ)
        end_time = make_timezone_aware(form.end_datetime.data, BRAZIL_TZ)

        is_available, conflicts = check_room_availability(
            form.room_id.data, start_time, end_time, exclude_meeting_id=meeting.id
        )
        if not is_available:
            conflict_info = []
            for c in conflicts:
                start_brazil = format_datetime_brazil(c.start_datetime)
                end_brazil = format_datetime_brazil(c.end_datetime)
                conflict_info.append(f"{c.title} ({start_brazil.strftime('%H:%M')} - {end_brazil.strftime('%H:%M')})")
            
            flash(f"Sala não disponível. Conflitos: {', '.join(conflict_info)}", "error")
            return render_template("meetings/edit.html", form=form, meeting=meeting)

        participant_ids = form.participants.data
        is_users_available, conflicting_users = check_user_availability(participant_ids, start_time, end_time, exclude_meeting_id=meeting.id)

        if not is_users_available:
            flash(f"Os seguintes usuários já possuem reuniões agendadas neste horário: {', '.join(conflicting_users)}.", "danger")
            return render_template("meetings/edit.html", form=form, meeting=meeting)

        participant_ids = form.participants.data
        selected_users = User.query.filter(User.id.in_(participant_ids)).all()
        participant_names = ", ".join([user.username for user in selected_users])

        # Atualizar os dados da reunião
        meeting.title = form.title.data
        meeting.description = form.description.data
        meeting.start_datetime = start_time
        meeting.end_datetime = end_time
        meeting.participants = participant_names
        meeting.room_id = form.room_id.data
        meeting.updated_at = get_brazil_now()

        # Lógica para lidar com reuniões recorrentes
        if form.edit_all_recurring.data:
            # Se o usuário optou por editar todas as ocorrências futuras
            parent_meeting = meeting
            if meeting.parent_meeting_id:
                parent_meeting = Meeting.query.get(meeting.parent_meeting_id)
                # Se a reunião editada não é a principal, mas é uma ocorrência, precisamos atualizar a principal
                if parent_meeting:
                    parent_meeting.title = form.title.data
                    parent_meeting.description = form.description.data
                    parent_meeting.room_id = form.room_id.data
                    parent_meeting.participants = participant_names
                    parent_meeting.recurrence_type = form.recurrence_type.data
                    parent_meeting.recurrence_end = form.recurrence_end.data
                    db.session.add(parent_meeting)

            # Deletar todas as ocorrências futuras da série recorrente (incluindo a atual se for a principal)
            Meeting.query.filter(
                (Meeting.parent_meeting_id == parent_meeting.id) | (Meeting.id == parent_meeting.id and parent_meeting.is_recurring),
                Meeting.start_datetime >= meeting.start_datetime
            ).delete(synchronize_session=False)
            db.session.commit()

            # Recriar a reunião principal (se for o caso) e as ocorrências futuras com base nas novas informações
            # A reunião principal precisa ser recriada para refletir as novas datas/horas/participantes
            new_parent_meeting = Meeting(
                title=form.title.data,
                description=form.description.data,
                room_id=form.room_id.data,
                start_datetime=start_time,
                end_datetime=end_time,
                is_recurring=form.is_recurring.data,
                recurrence_type=form.recurrence_type.data,
                recurrence_end=form.recurrence_end.data,
                created_by=current_user.id,
                participants=participant_names,
                created_at=get_brazil_now()
            )
            db.session.add(new_parent_meeting)
            db.session.commit()

            # Recriar as ocorrências futuras
            if new_parent_meeting.is_recurring:
                recurring_meetings = create_recurring_meetings(new_parent_meeting, start_time.time(), end_time.time())
                if recurring_meetings:
                    db.session.add_all(recurring_meetings)
                    db.session.commit()
                    print(f"✅ Atualizadas {len(recurring_meetings)} reuniões recorrentes.")

        else:
            # Se o usuário optou por editar apenas esta ocorrência
            db.session.add(meeting)
            db.session.commit()

        flash("Reunião atualizada com sucesso!", "success")
        return redirect(url_for("meetings.dashboard"))

    users = User.query.all()
    rooms = Room.query.all()
    form.participants.data = [user.id for user in meeting.participants_list]
    return render_template("meetings/edit.html", form=form, meeting=meeting, users=users, rooms=rooms)


@meetings_bp.route("/cancel_meeting/<int:meeting_id>", methods=["POST"])
@login_required
def cancel_meeting(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if meeting.created_by != current_user.id and not current_user.is_admin:
        flash("Você não tem permissão para cancelar esta reunião.", "error")
        return redirect(url_for("meetings.my_meetings"))

    recipients = [meeting.creator.email] + [user.email for user in meeting.participants_list]

    if meeting.is_recurring or meeting.parent_meeting_id:
        if meeting.parent_meeting_id:
            parent_meeting = Meeting.query.get(meeting.parent_meeting_id)
        else:
            parent_meeting = meeting

        if parent_meeting:
            Meeting.query.filter(
                Meeting.parent_meeting_id == parent_meeting.id,
                Meeting.start_datetime >= meeting.start_datetime
            ).delete(synchronize_session=False)
            db.session.commit()
            if parent_meeting.id == meeting.id:
                db.session.delete(parent_meeting)
                db.session.commit()

            flash(f'Todas as ocorrências futuras da reunião "{parent_meeting.title}" foram canceladas!', 'success')
            send_meeting_notification(parent_meeting, 'cancelled', recipients=recipients)
            create_meeting_notifications(parent_meeting, 'cancelled', participants_only=True)
            return redirect(url_for('meetings.my_meetings'))

    send_meeting_notification(meeting, 'cancelled', recipients=recipients)
    create_meeting_notifications(meeting, 'cancelled', participants_only=True)

    db.session.delete(meeting)
    db.session.commit()

    flash(f'Reunião "{meeting.title}" cancelada com sucesso!', 'success')
    return redirect(url_for('meetings.my_meetings'))


@meetings_bp.route('/api/check_availability')
@login_required
def check_availability():
    room_id = request.args.get('room_id', type=int)
    start_datetime = request.args.get('start_datetime')
    end_datetime = request.args.get('end_datetime')
    exclude_meeting_id = request.args.get('exclude_meeting_id', type=int)

    if not all([room_id, start_datetime, end_datetime]):
        return jsonify({'available': False, 'error': 'Parâmetros obrigatórios não fornecidos'})

    try:
        start_dt = parse_datetime_from_input(start_datetime)
        end_dt = parse_datetime_from_input(end_datetime)
        
        is_available, conflicts = check_room_availability(room_id, start_dt, end_dt, exclude_meeting_id)
        
        conflict_list = []
        for conflict in conflicts:
            start_brazil = format_datetime_brazil(conflict.start_datetime)
            end_brazil = format_datetime_brazil(conflict.end_datetime)
            conflict_list.append({
                'title': conflict.title,
                'start': start_brazil.strftime('%H:%M'),
                'end': end_brazil.strftime('%H:%M')
            })
        
        return jsonify({
            'available': is_available,
            'conflicts': conflict_list
        })
    except Exception as e:
        return jsonify({'available': False, 'error': str(e)})


@meetings_bp.route('/cancel_recurrence/<int:meeting_id>', methods=['POST'])
@login_required
def cancel_recurrence(meeting_id):
    original = Meeting.query.get_or_404(meeting_id)
    
    if original.created_by != current_user.id and not current_user.is_admin:
        flash('Você não tem permissão para cancelar esta recorrência.', 'error')
        return redirect(url_for('meetings.my_meetings'))

    recurring = Meeting.query.filter_by(parent_meeting_id=meeting_id).all()
    
    for m in recurring:
        db.session.delete(m)

    db.session.delete(original)
    db.session.commit()

    flash(f'Recorrência de "{original.title}" cancelada com sucesso!', 'success')
    return redirect(url_for('meetings.my_meetings'))


def check_user_availability(user_ids, start_datetime, end_datetime, exclude_meeting_id=None):
    print(f"🔍 Checando disponibilidade para usuários {user_ids} de {start_datetime} até {end_datetime}")
    conflicting_users = []
    for user_id in user_ids:
        user = User.query.get(user_id)
        if not user:
            continue

        query = Meeting.query.filter(
            Meeting.start_datetime < end_datetime,
            Meeting.end_datetime > start_datetime,
            (Meeting.created_by == user.id) | (Meeting.participants.like(f'%{user.username}%'))
        )
        if exclude_meeting_id:
            query = query.filter(Meeting.id != exclude_meeting_id)

        conflicts = query.all()
        if conflicts:
            conflicting_users.append(user.username)
            for conflict in conflicts:
                print(f"⚠️ Conflito para o usuário {user.username} com a reunião {conflict.title} de {conflict.start_datetime} até {conflict.end_datetime}")

    return len(conflicting_users) == 0, conflicting_users


@meetings_bp.route("/delete_expired_meetings", methods=["POST"])
@login_required
def delete_expired_meetings():
    if not current_user.is_admin:
        flash("Você não tem permissão para realizar esta ação.", "danger")
        return redirect(url_for("meetings.dashboard"))

    now_brazil = get_brazil_now()
    expired_meetings = Meeting.query.filter(Meeting.end_datetime < now_brazil).all()
    deleted_count = 0

    for meeting in expired_meetings:
        db.session.delete(meeting)
        deleted_count += 1

    db.session.commit()
    flash(f"{deleted_count} reuniões expiradas foram deletadas com sucesso.", "success")
    return redirect(url_for("meetings.dashboard"))



