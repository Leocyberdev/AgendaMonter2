from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
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
from datetime import datetime, timedelta, timezone
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo
import json

meetings_bp = Blueprint('meetings', __name__)



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


def create_recurring_meetings(base_meeting):
    if not base_meeting.is_recurring or not base_meeting.recurrence_type:
        return []

    if not base_meeting.recurrence_end:
        print("⚠️ Erro: recurrence_end está vazio para uma reunião recorrente.")
        return []

    created_meetings = []
    current_date = base_meeting.start_datetime.date() - timedelta(days=1) # Começa um dia antes para incluir o dia inicial no loop
    start_time_of_day = base_meeting.start_datetime.time()
    end_time_of_day = base_meeting.end_datetime.time()

    try:
        end_date = datetime.combine(
            base_meeting.recurrence_end,
            datetime.min.time(),
            tzinfo=ZoneInfo("America/Sao_Paulo")  # ← garantir datetime "aware"
        )
    except Exception as e:
        print(f"❌ Erro ao processar data de fim da recorrência: {e}")
        return []

    duration = base_meeting.end_datetime - base_meeting.start_datetime
    max_iterations = 100  # Limite de segurança para evitar loops infinitos
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


            # Para reuniões diárias, pular fins de semana (apenas dias úteis)
            if base_meeting.recurrence_type == 'daily':
                if current_date.weekday() >= 5:  # 5=sábado, 6=domingo
                    continue
            new_start_datetime = datetime.combine(current_date, start_time_of_day)
            new_start_datetime = make_timezone_aware(new_start_datetime, BRAZIL_TZ)

            new_end_datetime = datetime.combine(current_date, end_time_of_day)
            new_end_datetime = make_timezone_aware(new_end_datetime, BRAZIL_TZ)

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
                    is_recurring=False, # Reuniões filhas não são recorrentes por si só
                    created_at=get_brazil_now()  # Define created_at com horário do Brasil
                # Não enviar e-mails para reuniões filhas, apenas para a principal
                # A notificação da reunião principal deve cobrir a recorrência
                # send_email_notification=False # Adicionar um campo para controlar isso se necessário
                )
                db.session.add(new_meeting)
                created_meetings.append(new_meeting)

        except Exception as e:
            print(f"❌ Erro ao criar reunião recorrente para {current_date}: {e}")
            continue

    print(f"📅 Criando reunião para {current_date}")


    print(f"✅ Processadas {iteration_count} iterações, criadas {len(created_meetings)} reuniões")
    return created_meetings



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

    # Buscar notificações do usuário
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
    user = current_user  # Corrigido: user sempre definido
    form = MeetingForm()  # Corrigido: form definido na parte GET

    if form.validate_on_submit():
        start_time = form.start_datetime.data
        end_time = form.end_datetime.data
        room_id = form.room_id.data

        # Garante que os datetimes são timezone-aware
        start_time = make_timezone_aware(start_time, BRAZIL_TZ)
        end_time = make_timezone_aware(end_time, BRAZIL_TZ)

        # Validação usando timezone brasileiro
        if is_in_past(start_time):
            flash("A data e hora de início não pode ser no passado.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        if end_time <= start_time:
            flash("A data e hora de término deve ser depois da data e hora de início.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        # CORREÇÃO: usar nomes corretos dos campos do modelo
        existing_meetings = Meeting.query.filter(
            Meeting.room_id == room_id,
            Meeting.start_datetime < end_time,
            Meeting.end_datetime > start_time
        ).all()

        if existing_meetings:
            flash("A sala selecionada não está disponível neste horário.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        # Verifica a disponibilidade dos participantes
        participant_ids = form.participants.data
        is_users_available, conflicting_users = check_user_availability(participant_ids, start_time, end_time)

        if not is_users_available:
            flash(f"Os seguintes usuários já possuem reuniões agendadas neste horário: {', '.join(conflicting_users)}.", "danger")
            return redirect(url_for("meetings.create_meeting"))

        # Criar nova reunião com horário atual do Brasil
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

        # Converte a lista de IDs de participantes para nomes de usuário
        participant_names = []
        for participant_id in form.participants.data:
            participant = User.query.get(participant_id)
            if participant:
                participant_names.append(participant.username)

        # Define participants como string separada por vírgulas
        new_meeting.participants = ", ".join(participant_names) if participant_names else None

        db.session.add(new_meeting)
        db.session.commit()

        # Lista para armazenar todas as reuniões (principal + recorrentes)
        all_meetings = [new_meeting]

        # Criar reuniões recorrentes se necessário
        if new_meeting.is_recurring:
            try:
                recurring_meetings = create_recurring_meetings(new_meeting)
                if recurring_meetings:
                    db.session.commit()
                    all_meetings.extend(recurring_meetings)
                    print(f"✅ Criadas {len(recurring_meetings)} reuniões recorrentes")
            except Exception as e:
                print(f"❌ Erro ao criar reuniões recorrentes: {e}")
                # Não falha a operação, apenas registra o erro
                db.session.rollback()
                db.session.commit()  # Garante que a reunião principal seja salva

        # Enviar e-mails para os participantes APENAS da reunião principal
        if participant_names:
            # Buscar e-mails dos participantes
            participant_emails = []
            for participant_id in form.participants.data:
                participant = User.query.get(participant_id)
                if participant and participant.email:
                    participant_emails.append(participant.email)

            # Enviar notificação por e-mail APENAS para a reunião principal
            if participant_emails:
                try:
                    # Adapta o assunto e corpo do e-mail para reuniões recorrentes
                    if new_meeting.is_recurring:
                        subject_suffix = f" (Recorrente até {new_meeting.recurrence_end.strftime('%d/%m/%Y')})"
                        body_suffix = f"\nEsta é uma reunião recorrente que se repete {new_meeting.recurrence_type} até {new_meeting.recurrence_end.strftime('%d/%m/%Y')}."
                    else:
                        subject_suffix = ""
                        body_suffix = ""

                    message_body = f"""
Uma nova reunião foi agendada:

Título: {new_meeting.title}{subject_suffix}
Data: {new_meeting.start_datetime.strftime('%d/%m/%Y')}
Horário: {new_meeting.start_datetime.strftime('%H:%M')} - {new_meeting.end_datetime.strftime('%H:%M')}
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
                    # Não falha a operação, apenas registra o erro

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

    # Garantir que current_time seja timezone-aware
    current_time = get_brazil_now()

    # Garantir que os datetimes das reuniões sejam timezone-aware
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
        # Garante que os datetimes são timezone-aware
        start_time = make_timezone_aware(form.start_datetime.data, BRAZIL_TZ)
        end_time = make_timezone_aware(form.end_datetime.data, BRAZIL_TZ)

        is_available, conflicts = check_room_availability(
            form.room_id.data, start_time, end_time, exclude_meeting_id=meeting.id
        )
        if not is_available:
            conflict_info = [f"{c.title} ({c.start_datetime.strftime('%H:%M')} - {c.end_datetime.strftime('%H:%M')})" for c in conflicts]
            flash(f"Sala não disponível. Conflitos: {', '.join(conflict_info)}", "error")
            return render_template("meetings/edit.html", form=form, meeting=meeting)

        # Verifica a disponibilidade dos participantes
        participant_ids = form.participants.data
        is_users_available, conflicting_users = check_user_availability(participant_ids, start_time, end_time, exclude_meeting_id=meeting.id)

        if not is_users_available:
            flash(f"Os seguintes usuários já possuem reuniões agendadas neste horário: {', '.join(conflicting_users)}.", "danger")
            return render_template("meetings/edit.html", form=form, meeting=meeting)

        # Converte a lista de IDs de participantes para nomes de usuário
        participant_ids = form.participants.data
        selected_users = User.query.filter(User.id.in_(participant_ids)).all()
        participant_names = ", ".join([user.username for user in selected_users])

        meeting.title = form.title.data
        meeting.description = form.description.data
        meeting.start_datetime = start_time
        meeting.end_datetime = end_time
        meeting.participants = participant_names
        meeting.room_id = form.room_id.data
        meeting.updated_at = get_brazil_now()

        db.session.commit()
        flash(f'Reunião "{meeting.title}" atualizada com sucesso!', 'success')
        return redirect(url_for('meetings.my_meetings'))

    return render_template('meetings/edit.html', form=form, meeting=meeting)


from flask import abort

@meetings_bp.route('/delete/<int:meeting_id>', methods=['POST'])
@login_required
def delete_meeting(meeting_id):
    meeting = db.session.get(Meeting, meeting_id)
    if not meeting:
        abort(404)

    if meeting.created_by != current_user.id and not current_user.is_admin:
        flash('Você não tem permissão para deletar esta reunião.', 'error')
        return redirect(url_for('meetings.my_meetings'))

    # Excluir reuniões filhas (recorrentes)
    child_meetings = db.session.query(Meeting).filter_by(parent_meeting_id=meeting.id).all()
    for child in child_meetings:
        db.session.delete(child)

    # Notificações de cancelamento
    recipients = [
        user.email
        for user in db.session.query(User).filter(User.username.in_(meeting.get_participants_list())).all()
        if user.email
    ]
    send_meeting_notification(meeting, 'cancelled', recipients=recipients)
    create_meeting_notifications(meeting, 'cancelled', participants_only=True)

    # Deletar reunião principal
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
        return jsonify({'error': 'Parâmetros obrigatórios não fornecidos'}), 400

    try:
        # Parse das strings e garantir timezone-aware
        start_dt = datetime.fromisoformat(start_datetime)
        end_dt = datetime.fromisoformat(end_datetime)

        start_dt = make_timezone_aware(start_dt, BRAZIL_TZ)
        end_dt = make_timezone_aware(end_dt, BRAZIL_TZ)

        is_available, conflicts = check_room_availability(room_id, start_dt, end_dt, exclude_meeting_id)

        conflict_details = [{
            'title': c.title,
            'start': c.start_datetime.isoformat(),
            'end': c.end_datetime.isoformat()
        } for c in conflicts]

        return jsonify({'available': is_available, 'conflicts': conflict_details})

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@meetings_bp.route('/api/meeting_details/<int:meeting_id>')
@login_required
def meeting_details(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)

    # Garantir que os datetimes sejam timezone-aware para serialização
    start_dt = ensure_timezone_aware(meeting.start_datetime)
    end_dt = ensure_timezone_aware(meeting.end_datetime)

    return jsonify({
        'id': meeting.id,
        'title': meeting.title,
        'description': meeting.description,
        'start_datetime': start_dt.isoformat(),
        'end_datetime': end_dt.isoformat(),
        'participants': meeting.participants,
        'room_name': meeting.room.name,
        'creator_name': meeting.creator.username,
        'is_recurring': meeting.is_recurring,
        'recurrence_type': meeting.recurrence_type,
        'recurrence_end': meeting.recurrence_end.isoformat() if meeting.recurrence_end else None,
        'parent_meeting_id': meeting.parent_meeting_id
    })


@meetings_bp.route('/api/suggest_rooms')
@login_required
def suggest_rooms():
    start_datetime = request.args.get('start_datetime')
    end_datetime = request.args.get('end_datetime')

    if not all([start_datetime, end_datetime]):
        return jsonify({'error': 'Parâmetros obrigatórios não fornecidos'}), 400

    try:
        # Parse das strings e garantir timezone-aware
        start_dt = datetime.fromisoformat(start_datetime)
        end_dt = datetime.fromisoformat(end_datetime)

        start_dt = make_timezone_aware(start_dt, BRAZIL_TZ)
        end_dt = make_timezone_aware(end_dt, BRAZIL_TZ)

        available_rooms = []
        all_rooms = Room.query.filter_by(is_active=True).all()

        for room in all_rooms:
            is_available, _ = check_room_availability(room.id, start_dt, end_dt)
            if is_available:
                available_rooms.append(room.to_dict())

        return jsonify({'available_rooms': available_rooms})

    except Exception as e:
        return jsonify({'error': str(e)}), 400


@meetings_bp.route('/delete_series/<int:parent_id>', methods=['POST'])
@login_required
def delete_recurring_series(parent_id):
    original = Meeting.query.get_or_404(parent_id)

    if original.created_by != current_user.id and not current_user.is_admin:
        flash('Você não tem permissão para deletar esta série de reuniões.', 'error')
        return redirect(url_for('meetings.my_meetings'))

    # Reuniões filhas
    recurring = Meeting.query.filter_by(parent_meeting_id=parent_id).all()

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
        # Opcional: Adicionar lógica para não deletar reuniões recorrentes principais
        # se houver reuniões filhas futuras, ou apenas deletar as filhas expiradas.
        # Por simplicidade, aqui deletaremos todas as reuniões expiradas, incluindo pais.
        db.session.delete(meeting)
        deleted_count += 1

    db.session.commit()
    flash(f"{deleted_count} reuniões expiradas foram deletadas com sucesso.", "success")
    return redirect(url_for("meetings.dashboard"))