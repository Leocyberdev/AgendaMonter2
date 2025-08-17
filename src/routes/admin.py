from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import login_required, current_user
from src.models.user import db, User, Room
from src.models.meeting import Meeting

from src.forms import CreateUserForm
from functools import wraps

admin_bp = Blueprint('admin', __name__)

def admin_required(f):
    """Decorator para verificar se o usuário é administrador"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            flash('Acesso negado. Apenas administradores podem acessar esta página.', 'error')
            return redirect(url_for('meetings.dashboard'))
        return f(*args, **kwargs)
    return decorated_function

@admin_bp.route('/users')
@login_required
@admin_required
def users():
    """Lista todos os usuários do sistema"""
    all_users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=all_users)

@admin_bp.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    """Deleta um usuário do sistema"""
    user_to_delete = User.query.get_or_404(user_id)
    
    # Verificar se é o usuário Monter tentando deletar outro admin
    if user_to_delete.is_admin and current_user.username != 'Monter':
        flash('Apenas o usuário Monter pode excluir outros administradores.', 'error')
        return redirect(url_for('admin.users'))
    
    # Não permitir que o usuário delete a si mesmo
    if user_to_delete.id == current_user.id:
        flash('Você não pode excluir sua própria conta.', 'error')
        return redirect(url_for('admin.users'))
    
    # Verificar se o usuário tem reuniões associadas
    user_meetings = Meeting.query.filter_by(created_by=user_id).count()
    if user_meetings > 0:
        flash(f'Não é possível excluir o usuário {user_to_delete.username} pois ele possui {user_meetings} reunião(ões) associada(s).', 'error')
        return redirect(url_for('admin.users'))
    
    username = user_to_delete.username
    db.session.delete(user_to_delete)
    db.session.commit()
    
    flash(f'Usuário {username} excluído com sucesso!', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/toggle_admin/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def toggle_admin(user_id):
    """Alterna o status de administrador de um usuário"""
    user = User.query.get_or_404(user_id)
    
    # Apenas o usuário Monter pode alterar status de admin
    if current_user.username != 'Monter':
        flash('Apenas o usuário Monter pode alterar permissões de administrador.', 'error')
        return redirect(url_for('admin.users'))
    
    # Não permitir remover admin do próprio Monter
    if user.username == 'Monter' and user.is_admin:
        flash('Não é possível remover permissões de administrador do usuário Monter.', 'error')
        return redirect(url_for('admin.users'))
    
    user.is_admin = not user.is_admin
    db.session.commit()
    
    status = 'administrador' if user.is_admin else 'usuário comum'
    flash(f'Usuário {user.username} agora é {status}.', 'success')
    return redirect(url_for('admin.users'))

@admin_bp.route('/rooms')
@login_required
@admin_required
def rooms():
    """Lista todas as salas do sistema"""
    all_rooms = Room.query.order_by(Room.name).all()
    return render_template('admin/rooms.html', rooms=all_rooms)

@admin_bp.route('/meetings')
@login_required
@admin_required
def meetings():
    """Lista todas as reuniões do sistema"""
    all_meetings = Meeting.query.order_by(Meeting.start_datetime.desc()).all()
    return render_template('admin/meetings.html', meetings=all_meetings)

@admin_bp.route('/statistics')
@login_required
@admin_required
def statistics():
    """Exibe estatísticas do sistema"""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    
    # Estatísticas gerais
    total_users = User.query.count()
    total_meetings = Meeting.query.count()
    total_rooms = Room.query.filter_by(is_active=True).count()
    
    # Reuniões por mês (últimos 6 meses)
    six_months_ago = datetime.now() - timedelta(days=180)
    meetings_by_month = db.session.query(
        func.strftime('%Y-%m', Meeting.start_datetime).label('month'),
        func.count(Meeting.id).label('count')
    ).filter(
        Meeting.start_datetime >= six_months_ago
    ).group_by(
        func.strftime('%Y-%m', Meeting.start_datetime)
    ).all()

    
    # Salas mais utilizadas
    rooms_usage = db.session.query(
        Room.name,
        func.count(Meeting.id).label('count')
    ).join(Meeting).group_by(Room.name).order_by(func.count(Meeting.id).desc()).all()
    
    # Usuários mais ativos (que mais criam reuniões)
    active_users = db.session.query(
        User.username,
        func.count(Meeting.id).label('count')
    ).join(Meeting).group_by(User.username).order_by(func.count(Meeting.id).desc()).limit(5).all()
    
    return render_template('admin/statistics.html',
                         total_users=total_users,
                         total_meetings=total_meetings,
                         total_rooms=total_rooms,
                         meetings_by_month=meetings_by_month,
                         rooms_usage=rooms_usage,
                         active_users=active_users)

@admin_bp.route('/api/user_info/<int:user_id>')
@login_required
@admin_required
def user_info_api(user_id):
    """API para obter informações detalhadas de um usuário"""
    user = User.query.get_or_404(user_id)
    
    # Contar reuniões criadas pelo usuário
    meetings_count = Meeting.query.filter_by(created_by=user_id).count()
    
    # Últimas reuniões criadas
    recent_meetings = Meeting.query.filter_by(created_by=user_id).order_by(
        Meeting.created_at.desc()
    ).limit(5).all()
    
    user_data = user.to_dict()
    user_data['meetings_count'] = meetings_count
    user_data['recent_meetings'] = [meeting.to_dict() for meeting in recent_meetings]
    
    return jsonify(user_data)

