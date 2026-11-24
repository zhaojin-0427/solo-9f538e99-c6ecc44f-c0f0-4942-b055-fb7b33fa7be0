import os

from flask import Flask, g

from . import db as dbmod

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "star.db")


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("STAR_DB", DEFAULT_DB)
    app.secret_key = os.environ.get("SECRET_KEY", "variable-star-dev-key")

    conn = dbmod.connect(app.config["DB_PATH"])
    dbmod.init_db(conn)
    conn.close()

    @app.before_request
    def _open_db():
        g.db = dbmod.connect(app.config["DB_PATH"])

    @app.teardown_request
    def _close_db(exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    app.add_template_filter(lambda v: f"{v:,}", "num")

    from .core import epoch_to_iso

    app.add_template_filter(epoch_to_iso, "isoutc")

    from .routes import bp

    app.register_blueprint(bp)
    return app
