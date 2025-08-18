import os
import logging

# Configuração robusta de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Config:
    """Configurações base da aplicação"""
    
    # Chave secreta para sessões
    SECRET_KEY = os.environ.get("SECRET_KEY") or "monter-eletrica-sistema-reunioes-2024"
    SECURITY_PASSWORD_SALT = os.environ.get("SECURITY_PASSWORD_SALT") or "meu-salt-seguro-2024"
    
    # Configuração EXPLÍCITA do banco de dados
    if os.getenv("RENDER"):  # Ambiente Render - FORÇA PostgreSQL
        database_url = os.environ.get("DATABASE_URL")  # Obter URL do ambiente Render
        if not database_url:
            logger.error("❌ Variável de ambiente DATABASE_URL não encontrada no Render.")
            # Fallback para a URL hardcoded se a variável de ambiente não estiver definida
            database_url = "postgresql://agendamonter_56dt_user:Ex2NysbhR7k46rTI6NHoY7dkMM4Y7WMi@dpg-d25tnhili9vc739fgvtg-a/agendamonter_56dt"
        logger.info("✅ Modo Render - PostgreSQL configurado")
    else:
        # Desenvolvimento local (usar SQLite apenas se explicitamente definido)
        database_url = os.getenv("DATABASE_URL") or f"sqlite:///{os.path.join(os.path.dirname(__file__), "database", "app.db")}"
        logger.info("⚠️ Modo desenvolvimento - Verifique o banco de dados")

    # Garante substituição do esquema postgres:// se necessário
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    
    SQLALCHEMY_DATABASE_URI = database_url
    logger.info(f"📌 String de conexão: {SQLALCHEMY_DATABASE_URI}")
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Configuração do Flask-Mail
    MAIL_SERVER = os.environ.get("MAIL_SERVER") or "smtp.gmail.com"
    MAIL_PORT = int(os.environ.get("MAIL_PORT") or 587)
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() in ["true", "on", "1"]
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME") or "agendamontereletrica@gmail.com"
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD") or "cent dvbi wgxc acjd"
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER") or "agendamontereletrica@gmail.com"

    # DEBUG: Adicione este método para verificar conexões
    @classmethod
    def check_db_connection(cls):
        from sqlalchemy import create_engine
        try:
            engine = create_engine(cls.SQLALCHEMY_DATABASE_URI)
            conn = engine.connect()
            conn.close()
            logger.info("✔️ Conexão com banco de dados bem-sucedida")
            return True
        except Exception as e:
            logger.error(f"❌ Falha na conexão com o banco: {str(e)}")
            return False

class ProductionConfig(Config):
    """Configurações para produção"""
    DEBUG = False
    # Força verificação da conexão ao iniciar
    Config.check_db_connection()

class DevelopmentConfig(Config):
    """Configurações para desenvolvimento"""
    DEBUG = True

config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": ProductionConfig  # Padrão para produção
}
