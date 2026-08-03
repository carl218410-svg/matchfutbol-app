"""
MatchFutbol — MVP (Huacho / Norte Chico)
App tipo Trova para encontrar e inscribirse a partidos de futbol.

Stack: Flask + SQLite (sin dependencias externas mas alla de Flask).
Ejecutar:
    pip install -r requirements.txt
    python app.py
Luego abrir http://localhost:5000

Usuarios de ejemplo (ver _seed): password para todos es "huacho123".
Panel admin: /admin/login — password en env MATCHFUTBOL_ADMIN_PASSWORD
(por defecto "admin123" en el demo).
"""

import os
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask import (
    Flask, request, redirect, url_for, render_template, flash, jsonify,
    session, g,
)
from authlib.integrations.flask_client import OAuth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# En Railway/Render el disco del contenedor se borra en cada redeploy o
# reinicio salvo que montes un volumen persistente. MATCHFUTBOL_DB_PATH deja
# apuntar la base de datos a ese volumen (ej. "/data/matchfutbol.db") sin
# tocar codigo; si no se define, se usa el comportamiento original (el
# archivo vive junto a app.py, valido para correr en local).
DB_PATH = os.environ.get("MATCHFUTBOL_DB_PATH", os.path.join(BASE_DIR, "matchfutbol.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("MATCHFUTBOL_SECRET_KEY", "matchfutbol-huacho-demo")
# Railway (y la mayoria de PaaS) reciben el trafico por un proxy que termina
# el HTTPS y reenvia al contenedor por HTTP plano. Sin esto, Flask genera
# URLs externas (url_for(..., _external=True)) como "http://..." en vez de
# "https://...", lo que rompe el login con Google: el redirect_uri que le
# manda a Google no coincide con el que registraste (error 400
# redirect_uri_mismatch) porque el esquema no calza.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

PASSWORD_DEMO_DEFAULT = "huacho123"  # password de los usuarios sembrados en _seed()
ADMIN_PASSWORD = os.environ.get("MATCHFUTBOL_ADMIN_PASSWORD", "admin123")

# ---------------------------------------------------------------------------
# Login con Google (OAuth 2.0) — opcional: si no se configuran las
# credenciales, el boton "Continuar con Google" simplemente no se muestra
# (ver GOOGLE_LOGIN_ENABLED) y el resto de la app funciona igual con
# correo + contraseña.
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_ID = os.environ.get("MATCHFUTBOL_GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("MATCHFUTBOL_GOOGLE_CLIENT_SECRET")
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

oauth = OAuth(app)
if GOOGLE_LOGIN_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

# Posiciones estilo Trova: el arquero paga precio distinto, las otras tres
# son solo etiqueta/preferencia de juego (mismo precio de "jugador").
POSICIONES = {
    "arquero":   {"label": "Arquero",   "emoji": "🥅"},
    "defensa":   {"label": "Defensa",   "emoji": "🛡️"},
    "volante":   {"label": "Volante",   "emoji": "🎯"},
    "delantero": {"label": "Delantero", "emoji": "⚡"},
}

NIVELES = {
    "principiante": {"label": "Principiante", "emoji": "🌱"},
    "intermedio":   {"label": "Intermedio",   "emoji": "⚙️"},
    "avanzado":     {"label": "Avanzado",     "emoji": "🔥"},
}

TEMPORADA_ACTUAL = "2026-S2"  # temporada activa de la Liga MatchFutbol (manual)

# Formación fija por equipo (fulbito 6 vs 6): 1 arquero, 2 defensas,
# 2 volantes, 1 delantero = 6 jugadores por equipo, 12 en total por partido.
FORMACION = {"arquero": 1, "defensa": 2, "volante": 2, "delantero": 1}
JUGADORES_POR_EQUIPO = sum(FORMACION.values())
CUPOS_POR_PARTIDO = JUGADORES_POR_EQUIPO * 2


def wa_link(telefono, mensaje):
    """Genera un enlace wa.me con el mensaje ya escrito (envío manual, sin API)."""
    if not telefono:
        return "#"
    solo_digitos = "".join(ch for ch in telefono if ch.isdigit())
    if not solo_digitos.startswith("51"):
        solo_digitos = "51" + solo_digitos
    return f"https://wa.me/{solo_digitos}?text={urllib.parse.quote(mensaje)}"


app.jinja_env.globals["wa_link"] = wa_link
app.jinja_env.globals["POSICIONES"] = POSICIONES
app.jinja_env.globals["NIVELES"] = NIVELES
app.jinja_env.globals["FORMACION"] = FORMACION
app.jinja_env.globals["GOOGLE_LOGIN_ENABLED"] = GOOGLE_LOGIN_ENABLED


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_column_if_missing(conn, tabla, columna, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({tabla})").fetchall()]
    if columna not in cols:
        conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {ddl}")
        conn.commit()


def _migrar_telefono_opcional(conn):
    """El telefono es un dato mas del perfil (para WhatsApp), no una
    credencial de acceso: no debe ser obligatorio para registrarse (por
    ejemplo con Google, que no lo entrega). Antes era NOT NULL UNIQUE a
    nivel de tabla; SQLite no permite quitar esa restriccion con ALTER
    TABLE, asi que se reconstruye la tabla una sola vez si hace falta.
    Se llama despues de que todas las columnas nuevas (google_id,
    reset_token, etc.) ya existen, para poder copiarlas todas."""
    col = next(
        (r for r in conn.execute("PRAGMA table_info(usuarios)").fetchall()
         if r["name"] == "telefono"),
        None,
    )
    if col is None or col["notnull"] == 0:
        return  # ya es opcional
    columnas = (
        "id, nombre, telefono, email, password_hash, reset_token, "
        "reset_token_expira, google_id, posicion, nivel_juego, es_organizador, "
        "organizador_solicitado, es_dueno_cancha, dni, verificado, creado_en"
    )
    conn.executescript(f"""
        ALTER TABLE usuarios RENAME TO usuarios_old;
        CREATE TABLE usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            telefono TEXT,
            email TEXT,
            password_hash TEXT,
            reset_token TEXT,
            reset_token_expira TEXT,
            google_id TEXT,
            posicion TEXT NOT NULL DEFAULT 'volante',
            nivel_juego TEXT NOT NULL DEFAULT 'intermedio',
            es_organizador INTEGER NOT NULL DEFAULT 0,
            organizador_solicitado INTEGER NOT NULL DEFAULT 0,
            es_dueno_cancha INTEGER NOT NULL DEFAULT 0,
            dni TEXT,
            verificado INTEGER NOT NULL DEFAULT 0,
            creado_en TEXT NOT NULL
        );
        INSERT INTO usuarios ({columnas})
        SELECT {columnas} FROM usuarios_old;
        DROP TABLE usuarios_old;
    """)
    conn.commit()


def init_db():
    """Crea las tablas y carga datos de ejemplo si la BD no existe."""
    nueva = not os.path.exists(DB_PATH)
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            telefono TEXT,
            email TEXT,
            password_hash TEXT,
            reset_token TEXT,
            reset_token_expira TEXT,
            posicion TEXT NOT NULL DEFAULT 'volante',   -- 'arquero','defensa','volante','delantero'
            nivel_juego TEXT NOT NULL DEFAULT 'intermedio', -- 'principiante','intermedio','avanzado'
            es_organizador INTEGER NOT NULL DEFAULT 0,
            organizador_solicitado INTEGER NOT NULL DEFAULT 0,
            es_dueno_cancha INTEGER NOT NULL DEFAULT 0,
            dni TEXT,
            verificado INTEGER NOT NULL DEFAULT 0,
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS partidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organizador_id INTEGER NOT NULL,
            cancha TEXT NOT NULL,
            distrito TEXT NOT NULL,
            fecha TEXT NOT NULL,                         -- ISO: YYYY-MM-DD
            hora TEXT NOT NULL,                          -- HH:MM
            cupos_total INTEGER NOT NULL,
            precio_jugador REAL NOT NULL,
            precio_arquero REAL NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'masculino', -- 'masculino', 'femenino' o 'mixto'
            grupo_whatsapp TEXT,                         -- enlace de invitación chat.whatsapp.com/...
            estado TEXT NOT NULL DEFAULT 'activo',        -- 'activo' o 'cancelado'
            creado_en TEXT NOT NULL,
            FOREIGN KEY (organizador_id) REFERENCES usuarios(id)
        );

        CREATE TABLE IF NOT EXISTS inscripciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partido_id INTEGER NOT NULL,
            usuario_id INTEGER NOT NULL,
            rol TEXT NOT NULL,                           -- 'jugador' o 'arquero' (define el precio)
            posicion TEXT NOT NULL DEFAULT 'volante',     -- 'arquero','defensa','volante','delantero'
            equipo TEXT NOT NULL DEFAULT 'A',             -- 'A' o 'B'
            estado_pago TEXT NOT NULL DEFAULT 'pendiente', -- 'pendiente' o 'pagado'
            metodo_pago TEXT,                            -- 'yape' o 'efectivo'
            referencia_pago TEXT,
            creado_en TEXT NOT NULL,
            UNIQUE (partido_id, usuario_id),
            FOREIGN KEY (partido_id) REFERENCES partidos(id),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        );

        CREATE TABLE IF NOT EXISTS anuncios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anunciante TEXT NOT NULL,
            titulo TEXT NOT NULL,
            texto TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '📣',
            url TEXT,
            activo INTEGER NOT NULL DEFAULT 1,
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notificaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            partido_id INTEGER,
            tipo TEXT NOT NULL,     -- 'confirmacion', 'recordatorio', 'cupo_liberado'
            mensaje TEXT NOT NULL,
            leida INTEGER NOT NULL DEFAULT 0,
            creado_en TEXT NOT NULL,
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id),
            FOREIGN KEY (partido_id) REFERENCES partidos(id)
        );

        CREATE TABLE IF NOT EXISTS canchas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dueno_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            distrito TEXT NOT NULL,
            direccion TEXT,
            telefono_contacto TEXT,
            descripcion TEXT,
            horarios TEXT,
            creado_en TEXT NOT NULL,
            FOREIGN KEY (dueno_id) REFERENCES usuarios(id)
        );

        CREATE TABLE IF NOT EXISTS equipos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL UNIQUE,
            distrito TEXT NOT NULL,
            creado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cancha_id INTEGER NOT NULL,
            usuario_id INTEGER NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            nota TEXT,
            estado TEXT NOT NULL DEFAULT 'pendiente', -- 'pendiente','confirmada','rechazada'
            creado_en TEXT NOT NULL,
            FOREIGN KEY (cancha_id) REFERENCES canchas(id),
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
        );

        CREATE TABLE IF NOT EXISTS liga_tabla (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            equipo_id INTEGER NOT NULL,
            temporada TEXT NOT NULL,
            pj INTEGER NOT NULL DEFAULT 0,
            pg INTEGER NOT NULL DEFAULT 0,
            pe INTEGER NOT NULL DEFAULT 0,
            pp INTEGER NOT NULL DEFAULT 0,
            gf INTEGER NOT NULL DEFAULT 0,
            gc INTEGER NOT NULL DEFAULT 0,
            pts INTEGER NOT NULL DEFAULT 0,
            actualizado_en TEXT NOT NULL,
            UNIQUE (equipo_id, temporada),
            FOREIGN KEY (equipo_id) REFERENCES equipos(id)
        );
        """
    )
    conn.commit()

    # Migraciones suaves para bases de datos creadas con versiones anteriores.
    _add_column_if_missing(conn, "partidos", "categoria",
                            "categoria TEXT NOT NULL DEFAULT 'masculino'")
    _add_column_if_missing(conn, "partidos", "grupo_whatsapp", "grupo_whatsapp TEXT")
    _add_column_if_missing(conn, "partidos", "estado", "estado TEXT NOT NULL DEFAULT 'activo'")
    _add_column_if_missing(conn, "usuarios", "password_hash", "password_hash TEXT")
    _add_column_if_missing(conn, "usuarios", "dni", "dni TEXT")
    _add_column_if_missing(conn, "usuarios", "verificado",
                            "verificado INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "usuarios", "nivel_juego",
                            "nivel_juego TEXT NOT NULL DEFAULT 'intermedio'")
    _add_column_if_missing(conn, "usuarios", "organizador_solicitado",
                            "organizador_solicitado INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "usuarios", "es_dueno_cancha",
                            "es_dueno_cancha INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "usuarios", "email", "email TEXT")
    _add_column_if_missing(conn, "usuarios", "reset_token", "reset_token TEXT")
    _add_column_if_missing(conn, "usuarios", "reset_token_expira", "reset_token_expira TEXT")
    _add_column_if_missing(conn, "usuarios", "google_id", "google_id TEXT")
    # El telefono pasa a ser opcional (dato de perfil, no credencial de
    # acceso) — reconstruye la tabla si venia de una version anterior donde
    # era NOT NULL UNIQUE.
    _migrar_telefono_opcional(conn)
    # Indices unicos parciales: permiten muchos usuarios sin correo/telefono/
    # google_id (NULL) pero no permiten repetir un mismo valor entre cuentas.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_email ON usuarios(email) "
        "WHERE email IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_telefono ON usuarios(telefono) "
        "WHERE telefono IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_google_id ON usuarios(google_id) "
        "WHERE google_id IS NOT NULL"
    )
    # Los organizadores que ya existian (de versiones sin flujo de aprobacion)
    # se consideran ya aprobados, para no romper el acceso de quien ya usaba la app.
    conn.execute(
        "UPDATE usuarios SET organizador_solicitado = 1 "
        "WHERE es_organizador = 1 AND organizador_solicitado = 0"
    )
    conn.commit()
    _add_column_if_missing(conn, "inscripciones", "estado_pago",
                            "estado_pago TEXT NOT NULL DEFAULT 'pendiente'")
    _add_column_if_missing(conn, "inscripciones", "metodo_pago", "metodo_pago TEXT")
    _add_column_if_missing(conn, "inscripciones", "referencia_pago", "referencia_pago TEXT")
    tenia_posicion = "posicion" in [
        r["name"] for r in conn.execute("PRAGMA table_info(inscripciones)").fetchall()
    ]
    _add_column_if_missing(conn, "inscripciones", "posicion",
                            "posicion TEXT NOT NULL DEFAULT 'volante'")
    if not tenia_posicion:
        # Bases de datos previas solo tenian 'rol' (jugador/arquero); se usa
        # como posicion de partida (jugador generico -> volante).
        conn.execute("UPDATE inscripciones SET posicion = rol WHERE rol = 'arquero'")
        conn.execute("UPDATE inscripciones SET posicion = 'volante' WHERE rol != 'arquero'")
        conn.commit()
    tenia_equipo = "equipo" in [
        r["name"] for r in conn.execute("PRAGMA table_info(inscripciones)").fetchall()
    ]
    _add_column_if_missing(conn, "inscripciones", "equipo", "equipo TEXT NOT NULL DEFAULT 'A'")
    if not tenia_equipo:
        # Bases de datos previas no tenian equipo A/B: se reparte alternando
        # por partido, respetando el cupo de cada puesto en la formacion.
        for partido in conn.execute("SELECT id FROM partidos").fetchall():
            contador = {"A": {p: 0 for p in FORMACION}, "B": {p: 0 for p in FORMACION}}
            filas = conn.execute(
                "SELECT id, posicion FROM inscripciones WHERE partido_id = ? "
                "ORDER BY creado_en",
                (partido["id"],),
            ).fetchall()
            for fila in filas:
                pos = fila["posicion"] if fila["posicion"] in FORMACION else "volante"
                asignado = None
                for equipo in ("A", "B"):
                    if contador[equipo][pos] < FORMACION[pos]:
                        asignado = equipo
                        break
                if asignado is None:
                    asignado = "A"  # excedente (no deberia pasar en datos limpios)
                else:
                    contador[asignado][pos] += 1
                conn.execute(
                    "UPDATE inscripciones SET equipo = ? WHERE id = ?",
                    (asignado, fila["id"]),
                )
        conn.commit()

    # Normaliza el campo posicion de usuarios al esquema tipo Trova
    # (antes solo existia 'jugador' o 'arquero').
    conn.execute("UPDATE usuarios SET posicion = 'volante' WHERE posicion = 'jugador'")
    conn.commit()

    # Si hay usuarios sembrados de una version anterior sin password, se les
    # asigna el password demo para que el login siga funcionando.
    sin_password = conn.execute(
        "SELECT id FROM usuarios WHERE password_hash IS NULL"
    ).fetchall()
    if sin_password:
        hash_demo = generate_password_hash(PASSWORD_DEMO_DEFAULT)
        conn.executemany(
            "UPDATE usuarios SET password_hash = ? WHERE id = ?",
            [(hash_demo, r["id"]) for r in sin_password],
        )
        conn.commit()

    if nueva:
        _seed(conn)

    # Si la BD ya existia (sin anuncios), cargar anuncios de ejemplo una vez.
    hay_anuncios = conn.execute("SELECT COUNT(*) AS n FROM anuncios").fetchone()["n"]
    if hay_anuncios == 0:
        ahora = datetime.now().isoformat(timespec="seconds")
        anuncios = [
            ("Deportes El Crack", "20% dcto en chimpunes",
             "Muestra tu inscripcion y llevate 20% en toda la tienda.", "👟", "#"),
            ("Cevicheria La Ola", "Post-partido con causa",
             "2x1 en causas presentando tu partido de hoy en la app.", "🍽️", "#"),
            ("Agua Vital", "Hidratate en cancha",
             "Pack de agua a precio especial para equipos MatchFutbol.", "💧", "#"),
        ]
        conn.executemany(
            "INSERT INTO anuncios (anunciante, titulo, texto, emoji, url, activo, creado_en) "
            "VALUES (?,?,?,?,?,1,?)",
            [(*a, ahora) for a in anuncios],
        )
        conn.commit()

    conn.close()


def _seed(conn):
    """Datos de ejemplo ambientados en Huacho."""
    ahora = datetime.now().isoformat(timespec="seconds")
    hash_demo = generate_password_hash(PASSWORD_DEMO_DEFAULT)

    # nombre, telefono, posicion, nivel_juego, es_organizador, organizador_solicitado,
    # es_dueno_cancha, dni, verificado
    usuarios = [
        ("Carlos Rojas", "999111222", "volante", "avanzado", 1, 1, 0, "40011122", 1),
        ("Miguel Torres", "999333444", "arquero", "intermedio", 1, 1, 0, "40033344", 1),
        ("Lucia Fernandez", "999222333", "delantero", "avanzado", 1, 1, 0, "40022233", 0),
        ("Luis Campos", "999555666", "defensa", "principiante", 0, 0, 0, None, 0),
        ("Jose Ramirez", "999777888", "arquero", "intermedio", 0, 0, 0, None, 0),
        ("Andres Vela", "999999000", "delantero", "avanzado", 0, 0, 0, None, 0),
        ("Maria Quispe", "999444555", "volante", "intermedio", 0, 0, 0, None, 0),
        ("Rosa Diaz", "999666777", "arquero", "principiante", 0, 0, 0, None, 0),
        ("Pedro Salas", "999888111", "volante", "intermedio", 0, 0, 1, None, 0),  # dueño de cancha
        ("Sofia Meza", "999222888", "volante", "intermedio", 0, 1, 0, None, 0),  # solicitud pendiente
    ]

    def _email_demo(nombre):
        partes = nombre.lower().split()
        return f"{partes[0]}.{partes[-1]}@matchfutbol.demo"

    conn.executemany(
        "INSERT INTO usuarios (nombre, telefono, email, password_hash, posicion, nivel_juego, "
        "es_organizador, organizador_solicitado, es_dueno_cancha, dni, verificado, creado_en) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (n, t, _email_demo(n), hash_demo, p, nv, o, osol, edc, dni, v, ahora)
            for (n, t, p, nv, o, osol, edc, dni, v) in usuarios
        ],
    )

    partidos = [
        # organizador_id, cancha, distrito, fecha, hora, cupos, precio_jug, precio_arq, categoria
        (1, "Complejo El Golazo", "Huacho", "2026-08-02", "20:00", CUPOS_POR_PARTIDO, 10.0, 6.0, "masculino"),
        (1, "La Casa Cajamarquina", "Huacho", "2026-08-03", "19:00", CUPOS_POR_PARTIDO, 12.0, 7.0, "mixto"),
        (2, "Cancha Hualmay Sport", "Hualmay", "2026-08-04", "21:00", CUPOS_POR_PARTIDO, 10.0, 6.0, "masculino"),
        (3, "Complejo El Golazo", "Huacho", "2026-08-05", "18:00", CUPOS_POR_PARTIDO, 10.0, 6.0, "femenino"),
    ]
    conn.executemany(
        "INSERT INTO partidos (organizador_id, cancha, distrito, fecha, hora, "
        "cupos_total, precio_jugador, precio_arquero, categoria, creado_en) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(*p, ahora) for p in partidos],
    )

    # algunas inscripciones de ejemplo (ya pagadas, para que la demo se vea viva)
    # partido_id, usuario_id, rol, posicion, equipo, estado_pago, metodo_pago, referencia_pago
    inscripciones = [
        (1, 4, "jugador", "defensa", "A", "pagado", "efectivo", None),
        (1, 5, "arquero", "arquero", "B", "pagado", "yape", "OP123456"),
        (3, 6, "jugador", "delantero", "A", "pendiente", None, None),
        (4, 7, "jugador", "volante", "A", "pagado", "yape", "OP654321"),
        (4, 8, "arquero", "arquero", "B", "pendiente", None, None),
    ]
    conn.executemany(
        "INSERT INTO inscripciones (partido_id, usuario_id, rol, posicion, equipo, "
        "estado_pago, metodo_pago, referencia_pago, creado_en) VALUES (?,?,?,?,?,?,?,?,?)",
        [(*i, ahora) for i in inscripciones],
    )

    # anuncios de ejemplo (publicidad local vendida directamente)
    anuncios = [
        ("Deportes El Crack", "20% dcto en chimpunes",
         "Muestra tu inscripcion y llevate 20% en toda la tienda.", "👟", "#"),
        ("Cevicheria La Ola", "Post-partido con causa",
         "2x1 en causas presentando tu partido de hoy en la app.", "🍽️", "#"),
        ("Agua Vital", "Hidratate en cancha",
         "Pack de agua a precio especial para equipos MatchFutbol.", "💧", "#"),
    ]
    conn.executemany(
        "INSERT INTO anuncios (anunciante, titulo, texto, emoji, url, activo, creado_en) "
        "VALUES (?,?,?,?,?,1,?)",
        [(*a, ahora) for a in anuncios],
    )

    # Canchas de ejemplo (dueno_id 9 = Pedro Salas).
    canchas = [
        (9, "Complejo El Golazo", "Huacho", "Av. 28 de Julio 450",
         "999888111", "Grass sintético, 2 canchas de fulbito, vestuarios.",
         "Lun a dom 17:00-23:00"),
        (9, "Cancha Hualmay Sport", "Hualmay", "Jr. Los Pinos 210",
         "999888111", "Cancha techada, ideal para lluvia/sol fuerte.",
         "Lun a sáb 18:00-22:00"),
    ]
    conn.executemany(
        "INSERT INTO canchas (dueno_id, nombre, distrito, direccion, telefono_contacto, "
        "descripcion, horarios, creado_en) VALUES (?,?,?,?,?,?,?,?)",
        [(*c, ahora) for c in canchas],
    )

    # Reservas de ejemplo (usuario_id 4 = Luis Campos, 7 = Maria Quispe).
    cancha_ids = [r["id"] for r in conn.execute("SELECT id FROM canchas ORDER BY id").fetchall()]
    reservas = [
        (cancha_ids[0], 4, "2026-08-06", "19:00", "Cumpleaños, seremos 12", "pendiente"),
        (cancha_ids[1], 7, "2026-08-07", "20:00", None, "confirmada"),
    ]
    conn.executemany(
        "INSERT INTO reservas (cancha_id, usuario_id, fecha, hora, nota, estado, creado_en) "
        "VALUES (?,?,?,?,?,?,?)",
        [(*r, ahora) for r in reservas],
    )

    # Liga MatchFutbol Huacho — tabla de posiciones de ejemplo.
    equipos = [
        ("Deportivo Huacho", "Huacho"),
        ("Atlético Hualmay", "Hualmay"),
        ("Real Carquín", "Carquín"),
        ("Unión Santa María", "Santa María"),
    ]
    conn.executemany(
        "INSERT INTO equipos (nombre, distrito, creado_en) VALUES (?,?,?)",
        [(*e, ahora) for e in equipos],
    )
    # pj, pg, pe, pp, gf, gc (pts se calcula solo)
    stats = [(4, 3, 1, 0, 12, 5), (4, 2, 1, 1, 9, 7), (4, 1, 2, 1, 6, 6), (4, 0, 2, 2, 4, 9)]
    equipo_ids = [r["id"] for r in conn.execute("SELECT id FROM equipos ORDER BY id").fetchall()]
    for eid, (pj, pg, pe, pp, gf, gc) in zip(equipo_ids, stats):
        pts = pg * 3 + pe
        conn.execute(
            "INSERT INTO liga_tabla (equipo_id, temporada, pj, pg, pe, pp, gf, gc, pts, "
            "actualizado_en) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, TEMPORADA_ACTUAL, pj, pg, pe, pp, gf, gc, pts, ahora),
        )

    conn.commit()


# ---------------------------------------------------------------------------
# Autenticacion / sesion
# ---------------------------------------------------------------------------
@app.before_request
def cargar_usuario_actual():
    g.usuario = None
    g.notif_no_leidas = 0
    usuario_id = session.get("usuario_id")
    if usuario_id:
        conn = get_db()
        g.usuario = conn.execute(
            "SELECT * FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if g.usuario:
            g.notif_no_leidas = conn.execute(
                "SELECT COUNT(*) AS n FROM notificaciones WHERE usuario_id = ? AND leida = 0",
                (usuario_id,),
            ).fetchone()["n"]
        conn.close()


@app.context_processor
def inyectar_usuario():
    return {
        "usuario_actual": g.get("usuario"),
        "notif_no_leidas": g.get("notif_no_leidas", 0),
        "es_admin": session.get("is_admin", False),
    }


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.usuario:
            flash("Inicia sesión para continuar.", "error")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def organizador_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.usuario:
            flash("Inicia sesión para continuar.", "error")
            return redirect(url_for("login", next=request.path))
        if not g.usuario["es_organizador"]:
            flash("Solo organizadores pueden crear partidos.", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def dueno_cancha_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.usuario:
            flash("Inicia sesión para continuar.", "error")
            return redirect(url_for("login", next=request.path))
        if not g.usuario["es_dueno_cancha"]:
            flash("Esta sección es solo para dueños de cancha registrados.", "error")
            return redirect(url_for("canchas"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Ingresa como admin para ver esta página.", "error")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn = get_db()
        usuario = conn.execute(
            "SELECT * FROM usuarios WHERE email = ?", (email,)
        ).fetchone()
        conn.close()
        if usuario and usuario["password_hash"] and check_password_hash(
            usuario["password_hash"], password
        ):
            session.clear()
            session["usuario_id"] = usuario["id"]
            flash(f"Bienvenido, {usuario['nombre']}.", "ok")
            destino = request.form.get("next") or url_for("index")
            return redirect(destino)
        flash("Correo o contraseña incorrectos.", "error")
    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "ok")
    return redirect(url_for("index"))


@app.route("/auth/google")
def auth_google():
    if not GOOGLE_LOGIN_ENABLED:
        flash("El login con Google no está configurado en este servidor.", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("auth_google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def auth_google_callback():
    if not GOOGLE_LOGIN_ENABLED:
        return redirect(url_for("login"))
    try:
        token = oauth.google.authorize_access_token()
        perfil = token.get("userinfo")
        if not perfil:
            perfil = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo").json()
    except Exception:
        flash("No se pudo completar el inicio de sesión con Google. Intenta de nuevo.", "error")
        return redirect(url_for("login"))
    email = (perfil.get("email") or "").strip().lower()
    google_id = perfil.get("sub")
    nombre = perfil.get("name") or (email.split("@")[0] if email else "Jugador")
    if not email:
        flash("Tu cuenta de Google no tiene un correo disponible para registrar.", "error")
        return redirect(url_for("login"))

    conn = get_db()
    usuario = conn.execute("SELECT * FROM usuarios WHERE email = ?", (email,)).fetchone()
    if usuario:
        if not usuario["google_id"]:
            conn.execute("UPDATE usuarios SET google_id = ? WHERE id = ?", (google_id, usuario["id"]))
            conn.commit()
        conn.close()
        session.clear()
        session["usuario_id"] = usuario["id"]
        flash(f"Bienvenido, {usuario['nombre']}.", "ok")
        return redirect(url_for("index"))

    # Cuenta nueva vía Google: se crea directo, sin pedir teléfono ni nada
    # más — el teléfono es un dato de perfil (para WhatsApp), no una
    # credencial de acceso, así que se puede agregar después si se quiere.
    cur = conn.execute(
        "INSERT INTO usuarios (nombre, telefono, email, google_id, posicion, "
        "nivel_juego, es_organizador, organizador_solicitado, es_dueno_cancha, "
        "verificado, creado_en) VALUES (?,NULL,?,?,?,?,0,0,0,0,?)",
        (
            nombre,
            email,
            google_id,
            "volante",
            "intermedio",
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    session.clear()
    session["usuario_id"] = cur.lastrowid
    conn.close()
    flash(
        "Cuenta creada con Google. Si quieres recibir avisos por WhatsApp, "
        "agrega tu teléfono en tu perfil cuando quieras.", "ok",
    )
    return redirect(url_for("index"))


@app.route("/recuperar", methods=["GET", "POST"])
def recuperar_password():
    enlace = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_db()
        usuario = conn.execute(
            "SELECT * FROM usuarios WHERE email = ?", (email,)
        ).fetchone()
        if usuario is None:
            conn.close()
            flash("No encontramos ninguna cuenta con ese correo.", "error")
            return render_template("recuperar.html", enlace=None)
        token = secrets.token_urlsafe(32)
        expira = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE usuarios SET reset_token = ?, reset_token_expira = ? WHERE id = ?",
            (token, expira, usuario["id"]),
        )
        conn.commit()
        conn.close()
        enlace = url_for("restablecer_password", token=token, _external=True)
        flash(
            "Enlace de restablecimiento generado (modo demo: en producción esto "
            "llegaría a tu correo, no se mostraría aquí).", "ok",
        )
    return render_template("recuperar.html", enlace=enlace)


@app.route("/restablecer/<token>", methods=["GET", "POST"])
def restablecer_password(token):
    conn = get_db()
    usuario = conn.execute(
        "SELECT * FROM usuarios WHERE reset_token = ?", (token,)
    ).fetchone()
    valido = (
        usuario is not None
        and usuario["reset_token_expira"]
        and usuario["reset_token_expira"] >= datetime.now().isoformat(timespec="seconds")
    )
    if not valido:
        conn.close()
        flash("Ese enlace de restablecimiento no es válido o ya venció.", "error")
        return redirect(url_for("recuperar_password"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmar = request.form.get("confirmar_password", "")
        if len(password) < 6:
            conn.close()
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
            return render_template("restablecer.html", token=token)
        if password != confirmar:
            conn.close()
            flash("Las contraseñas no coinciden.", "error")
            return render_template("restablecer.html", token=token)
        conn.execute(
            "UPDATE usuarios SET password_hash = ?, reset_token = NULL, "
            "reset_token_expira = NULL WHERE id = ?",
            (generate_password_hash(password), usuario["id"]),
        )
        conn.commit()
        conn.close()
        flash("Contraseña actualizada. Ya puedes iniciar sesión.", "ok")
        return redirect(url_for("login"))
    conn.close()
    return render_template("restablecer.html", token=token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def partido_con_cupos(conn, partido_id):
    p = conn.execute("SELECT * FROM partidos WHERE id = ?", (partido_id,)).fetchone()
    if p is None:
        return None
    ocupados = conn.execute(
        "SELECT COUNT(*) AS n FROM inscripciones WHERE partido_id = ?", (partido_id,)
    ).fetchone()["n"]
    d = dict(p)
    d["ocupados"] = ocupados
    d["disponibles"] = p["cupos_total"] - ocupados
    org = conn.execute(
        "SELECT nombre, verificado FROM usuarios WHERE id = ?", (p["organizador_id"],)
    ).fetchone()
    d["organizador"] = org["nombre"] if org else "?"
    d["organizador_verificado"] = bool(org["verificado"]) if org else False
    return d


def plantilla_partido(conn, partido_id):
    """Arma la plantilla de los dos equipos (A y B) con sus puestos fijos
    (1 arquero, 2 defensas, 2 volantes, 1 delantero cada uno). Cada puesto es
    una lista de tamaño fijo: None si está libre, o la fila del inscrito."""
    filas = conn.execute(
        "SELECT i.id AS inscripcion_id, i.equipo, i.posicion, i.estado_pago, "
        "u.id AS usuario_id, u.nombre, u.telefono "
        "FROM inscripciones i JOIN usuarios u ON u.id = i.usuario_id "
        "WHERE i.partido_id = ? ORDER BY i.creado_en",
        (partido_id,),
    ).fetchall()
    plantilla = {
        equipo: {pos: [None] * cupo for pos, cupo in FORMACION.items()}
        for equipo in ("A", "B")
    }
    for f in filas:
        equipo, pos = f["equipo"], f["posicion"]
        if equipo in plantilla and pos in plantilla[equipo]:
            slots = plantilla[equipo][pos]
            for idx, slot in enumerate(slots):
                if slot is None:
                    slots[idx] = f
                    break
    return plantilla, filas


def crear_notificacion(conn, usuario_id, tipo, mensaje, partido_id=None):
    conn.execute(
        "INSERT INTO notificaciones (usuario_id, partido_id, tipo, mensaje, leida, creado_en) "
        "VALUES (?,?,?,?,0,?)",
        (usuario_id, partido_id, tipo, mensaje, datetime.now().isoformat(timespec="seconds")),
    )


# ---------------------------------------------------------------------------
# Rutas web — partidos
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    categoria = request.args.get("categoria", "todos")
    distrito = request.args.get("distrito", "todos")
    hoy = datetime.now().strftime("%Y-%m-%d")
    desde = request.args.get("desde", "").strip() or hoy
    hasta = request.args.get("hasta", "").strip()
    conn = get_db()

    condiciones = ["estado = 'activo'", "fecha >= ?"]
    parametros = [desde]
    if categoria in ("masculino", "femenino", "mixto"):
        condiciones.append("categoria = ?")
        parametros.append(categoria)
    if distrito != "todos":
        condiciones.append("distrito = ?")
        parametros.append(distrito)
    if hasta:
        condiciones.append("fecha <= ?")
        parametros.append(hasta)

    partidos_raw = conn.execute(
        f"SELECT id FROM partidos WHERE {' AND '.join(condiciones)} ORDER BY fecha, hora",
        parametros,
    ).fetchall()
    partidos = [partido_con_cupos(conn, r["id"]) for r in partidos_raw]
    distritos = [
        r["distrito"] for r in
        conn.execute("SELECT DISTINCT distrito FROM partidos ORDER BY distrito").fetchall()
    ]
    # anuncios patrocinados: hasta 3 activos para el carrusel
    anuncios = conn.execute(
        "SELECT * FROM anuncios WHERE activo = 1 ORDER BY id LIMIT 3"
    ).fetchall()
    conn.close()
    return render_template(
        "index.html", partidos=partidos, categoria=categoria, distrito=distrito,
        desde=desde, hasta=hasta, distritos=distritos, anuncios=anuncios,
    )


@app.route("/partido/<int:partido_id>")
def ver_partido(partido_id):
    conn = get_db()
    partido = partido_con_cupos(conn, partido_id)
    if partido is None:
        conn.close()
        flash("Ese partido no existe.", "error")
        return redirect(url_for("index"))
    plantilla, inscritos = plantilla_partido(conn, partido_id)
    mi_inscripcion = None
    if g.usuario:
        mi_inscripcion = conn.execute(
            "SELECT * FROM inscripciones WHERE partido_id = ? AND usuario_id = ?",
            (partido_id, g.usuario["id"]),
        ).fetchone()
    conn.close()
    return render_template(
        "partido.html",
        partido=partido,
        plantilla=plantilla,
        inscritos=inscritos,
        mi_inscripcion=mi_inscripcion,
    )


@app.route("/inscribir", methods=["POST"])
@login_required
def inscribir():
    partido_id = int(request.form["partido_id"])
    equipo, _, posicion = request.form.get("slot", "A:volante").partition(":")
    if equipo not in ("A", "B"):
        equipo = "A"
    if posicion not in POSICIONES:
        posicion = "volante"
    rol = "arquero" if posicion == "arquero" else "jugador"
    usuario_id = g.usuario["id"]
    conn = get_db()
    partido = partido_con_cupos(conn, partido_id)
    if partido is None:
        conn.close()
        flash("Ese partido no existe.", "error")
        return redirect(url_for("index"))
    if partido["estado"] != "activo":
        conn.close()
        flash("Ese partido fue cancelado por el organizador.", "error")
        return redirect(url_for("ver_partido", partido_id=partido_id))
    if partido["disponibles"] <= 0:
        conn.close()
        flash("Ya no hay cupos en ese partido.", "error")
        return redirect(url_for("ver_partido", partido_id=partido_id))
    ocupados_slot = conn.execute(
        "SELECT COUNT(*) AS n FROM inscripciones WHERE partido_id = ? AND equipo = ? "
        "AND posicion = ?",
        (partido_id, equipo, posicion),
    ).fetchone()["n"]
    if ocupados_slot >= FORMACION[posicion]:
        conn.close()
        flash(
            f"Ese puesto ({POSICIONES[posicion]['label']}) ya está lleno en el equipo "
            f"{equipo}. Elige otro puesto o equipo.", "error",
        )
        return redirect(url_for("ver_partido", partido_id=partido_id))
    try:
        cur = conn.execute(
            "INSERT INTO inscripciones (partido_id, usuario_id, rol, posicion, equipo, "
            "estado_pago, creado_en) VALUES (?,?,?,?,?,'pendiente',?)",
            (
                partido_id, usuario_id, rol, posicion, equipo,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        inscripcion_id = cur.lastrowid
        precio = partido["precio_arquero"] if rol == "arquero" else partido["precio_jugador"]
        mensaje_confirmacion = (
            f"Inscripción confirmada como {POSICIONES[posicion]['label']} del Equipo {equipo} "
            f"en {partido['cancha']} ({partido['fecha']} {partido['hora']}). "
            f"A pagar: S/ {precio:.2f}."
        )
        if partido.get("grupo_whatsapp"):
            mensaje_confirmacion += f" Únete al grupo del partido: {partido['grupo_whatsapp']}"
        crear_notificacion(
            conn, usuario_id, "confirmacion", mensaje_confirmacion, partido_id=partido_id,
        )
        conn.commit()
        flash(
            f"Inscripción confirmada como {POSICIONES[posicion]['label']} del Equipo {equipo}. "
            "Falta registrar el pago.", "ok",
        )
        conn.close()
        return redirect(url_for("pagar", inscripcion_id=inscripcion_id))
    except sqlite3.IntegrityError:
        conn.close()
        flash("Ya estás inscrito en este partido.", "error")
        return redirect(url_for("ver_partido", partido_id=partido_id))


@app.route("/pago/<int:inscripcion_id>", methods=["GET", "POST"])
@login_required
def pagar(inscripcion_id):
    conn = get_db()
    insc = conn.execute(
        "SELECT * FROM inscripciones WHERE id = ?", (inscripcion_id,)
    ).fetchone()
    if insc is None or insc["usuario_id"] != g.usuario["id"]:
        conn.close()
        flash("No se encontró esa inscripción.", "error")
        return redirect(url_for("index"))
    partido = partido_con_cupos(conn, insc["partido_id"])
    precio = partido["precio_arquero"] if insc["rol"] == "arquero" else partido["precio_jugador"]

    if request.method == "POST":
        metodo = request.form.get("metodo_pago", "efectivo")
        referencia = request.form.get("referencia_pago", "").strip() or None
        if metodo == "yape" and not referencia:
            conn.close()
            flash("Ingresa el código de operación de Yape para confirmar el pago.", "error")
            return redirect(url_for("pagar", inscripcion_id=inscripcion_id))
        conn.execute(
            "UPDATE inscripciones SET estado_pago = 'pagado', metodo_pago = ?, "
            "referencia_pago = ? WHERE id = ?",
            (metodo, referencia, inscripcion_id),
        )
        crear_notificacion(
            conn, g.usuario["id"], "confirmacion",
            f"Pago registrado ({metodo}) por S/ {precio:.2f} para el partido en "
            f"{partido['cancha']}.",
            partido_id=partido["id"],
        )
        conn.commit()
        conn.close()
        flash("Pago registrado. ¡Nos vemos en la cancha!", "ok")
        return redirect(url_for("ver_partido", partido_id=partido["id"]))

    conn.close()
    return render_template("pago.html", insc=insc, partido=partido, precio=precio)


@app.route("/cancelar/<int:inscripcion_id>", methods=["POST"])
@login_required
def cancelar_inscripcion(inscripcion_id):
    conn = get_db()
    insc = conn.execute(
        "SELECT * FROM inscripciones WHERE id = ?", (inscripcion_id,)
    ).fetchone()
    if insc is None or insc["usuario_id"] != g.usuario["id"]:
        conn.close()
        flash("No se encontró esa inscripción.", "error")
        return redirect(url_for("index"))
    partido = partido_con_cupos(conn, insc["partido_id"])
    conn.execute("DELETE FROM inscripciones WHERE id = ?", (inscripcion_id,))
    # Notifica al organizador que se liberó un cupo.
    crear_notificacion(
        conn, partido["organizador_id"], "cupo_liberado",
        f"{g.usuario['nombre']} canceló su cupo de {insc['rol']} en {partido['cancha']} "
        f"({partido['fecha']} {partido['hora']}).",
        partido_id=partido["id"],
    )
    conn.commit()
    conn.close()
    flash("Inscripción cancelada. Se liberó tu cupo.", "ok")
    return redirect(url_for("ver_partido", partido_id=partido["id"]))


@app.route("/partido/<int:partido_id>/grupo", methods=["POST"])
@organizador_required
def actualizar_grupo_whatsapp(partido_id):
    conn = get_db()
    partido = partido_con_cupos(conn, partido_id)
    if partido is None or partido["organizador_id"] != g.usuario["id"]:
        conn.close()
        flash("Solo el organizador del partido puede editar el grupo.", "error")
        return redirect(url_for("index"))
    enlace = request.form.get("grupo_whatsapp", "").strip() or None
    if enlace and "chat.whatsapp.com" not in enlace:
        conn.close()
        flash(
            "Ese no parece un enlace de invitación de WhatsApp (debe empezar con "
            "https://chat.whatsapp.com/...).", "error",
        )
        return redirect(url_for("ver_partido", partido_id=partido_id))
    conn.execute(
        "UPDATE partidos SET grupo_whatsapp = ? WHERE id = ?", (enlace, partido_id)
    )
    # Avisa a los ya inscritos que hay grupo nuevo para que se unan.
    if enlace:
        inscritos = conn.execute(
            "SELECT usuario_id FROM inscripciones WHERE partido_id = ?", (partido_id,)
        ).fetchall()
        for i in inscritos:
            crear_notificacion(
                conn, i["usuario_id"], "confirmacion",
                f"El organizador creó un grupo de WhatsApp para el partido en "
                f"{partido['cancha']} ({partido['fecha']} {partido['hora']}). Únete: {enlace}",
                partido_id=partido_id,
            )
    conn.commit()
    conn.close()
    flash("Grupo de WhatsApp actualizado.", "ok")
    return redirect(url_for("ver_partido", partido_id=partido_id))


@app.route("/partido/<int:partido_id>/editar", methods=["GET", "POST"])
@organizador_required
def editar_partido(partido_id):
    conn = get_db()
    partido = partido_con_cupos(conn, partido_id)
    if partido is None or partido["organizador_id"] != g.usuario["id"]:
        conn.close()
        flash("Solo el organizador del partido puede editarlo.", "error")
        return redirect(url_for("index"))
    if partido["estado"] == "cancelado":
        conn.close()
        flash("Este partido está cancelado y ya no se puede editar.", "error")
        return redirect(url_for("ver_partido", partido_id=partido_id))
    if request.method == "POST":
        try:
            categoria = request.form.get("categoria", partido["categoria"])
            if categoria not in ("masculino", "femenino", "mixto"):
                categoria = partido["categoria"]
            cancha_nueva = request.form["cancha"].strip()
            fecha_nueva = request.form["fecha"]
            hora_nueva = request.form["hora"]
            cambio_horario = (
                cancha_nueva != partido["cancha"] or fecha_nueva != partido["fecha"]
                or hora_nueva != partido["hora"]
            )
            conn.execute(
                "UPDATE partidos SET cancha=?, distrito=?, fecha=?, hora=?, "
                "precio_jugador=?, precio_arquero=?, categoria=? WHERE id=?",
                (
                    cancha_nueva,
                    request.form["distrito"].strip(),
                    fecha_nueva,
                    hora_nueva,
                    float(request.form["precio_jugador"]),
                    float(request.form["precio_arquero"]),
                    categoria,
                    partido_id,
                ),
            )
            if cambio_horario:
                inscritos = conn.execute(
                    "SELECT usuario_id FROM inscripciones WHERE partido_id = ?",
                    (partido_id,),
                ).fetchall()
                for i in inscritos:
                    crear_notificacion(
                        conn, i["usuario_id"], "recordatorio",
                        f"El organizador actualizó el partido: ahora es en {cancha_nueva} "
                        f"el {fecha_nueva} a las {hora_nueva}. Revisa que te siga sirviendo.",
                        partido_id=partido_id,
                    )
            conn.commit()
            flash("Partido actualizado.", "ok")
            conn.close()
            return redirect(url_for("ver_partido", partido_id=partido_id))
        except (ValueError, KeyError):
            flash("Revisa los datos del formulario.", "error")
    conn.close()
    return render_template("editar_partido.html", partido=partido)


@app.route("/partido/<int:partido_id>/cancelar", methods=["GET", "POST"])
@organizador_required
def cancelar_partido(partido_id):
    conn = get_db()
    partido = partido_con_cupos(conn, partido_id)
    if partido is None or partido["organizador_id"] != g.usuario["id"]:
        conn.close()
        flash("Solo el organizador del partido puede cancelarlo.", "error")
        return redirect(url_for("index"))
    inscritos = conn.execute(
        "SELECT u.id AS usuario_id, u.nombre, u.telefono FROM inscripciones i "
        "JOIN usuarios u ON u.id = i.usuario_id WHERE i.partido_id = ?",
        (partido_id,),
    ).fetchall()
    if request.method == "POST" and partido["estado"] == "activo":
        motivo = request.form.get("motivo", "").strip()
        conn.execute("UPDATE partidos SET estado = 'cancelado' WHERE id = ?", (partido_id,))
        for i in inscritos:
            mensaje = (
                f"El partido en {partido['cancha']} del {partido['fecha']} "
                f"{partido['hora']} fue CANCELADO por el organizador."
            )
            if motivo:
                mensaje += f" Motivo: {motivo}"
            crear_notificacion(conn, i["usuario_id"], "cupo_liberado", mensaje, partido_id=partido_id)
        conn.commit()
        flash(
            "Partido cancelado y aviso guardado para los inscritos. Usa los enlaces de "
            "WhatsApp de abajo para avisarles también a su celular.", "ok",
        )
        partido = partido_con_cupos(conn, partido_id)
    mensaje_wa = (
        f"Aviso: el partido en {partido['cancha']} del {partido['fecha']} {partido['hora']} "
        "fue cancelado. Disculpa las molestias."
    )
    links = [
        {"nombre": i["nombre"], "telefono": i["telefono"], "wa": wa_link(i["telefono"], mensaje_wa)}
        for i in inscritos
    ]
    conn.close()
    return render_template("cancelar_partido.html", partido=partido, links=links)


@app.route("/partido/<int:partido_id>/recordatorio", methods=["GET", "POST"])
@organizador_required
def recordatorio(partido_id):
    conn = get_db()
    partido = partido_con_cupos(conn, partido_id)
    if partido is None or partido["organizador_id"] != g.usuario["id"]:
        conn.close()
        flash("Solo el organizador del partido puede enviar recordatorios.", "error")
        return redirect(url_for("index"))
    inscritos = conn.execute(
        "SELECT i.id, u.id AS usuario_id, u.nombre, u.telefono, i.posicion "
        "FROM inscripciones i JOIN usuarios u ON u.id = i.usuario_id "
        "WHERE i.partido_id = ?",
        (partido_id,),
    ).fetchall()
    mensaje = (
        f"⚽ Recordatorio MatchFutbol: tu partido es el {partido['fecha']} a las "
        f"{partido['hora']} en {partido['cancha']} ({partido['distrito']}). ¡No faltes!"
    )
    if partido.get("grupo_whatsapp"):
        mensaje += f" Únete al grupo: {partido['grupo_whatsapp']}"
    if request.method == "POST":
        for i in inscritos:
            crear_notificacion(
                conn, i["usuario_id"], "recordatorio", mensaje, partido_id=partido_id
            )
        conn.commit()
        flash(
            f"Recordatorio guardado en la app para {len(inscritos)} inscrito(s). "
            "Usa los enlaces de WhatsApp de abajo para que también les llegue al celular.",
            "ok",
        )
    links = [
        {"nombre": i["nombre"], "posicion": i["posicion"], "telefono": i["telefono"],
         "wa": wa_link(i["telefono"], mensaje)}
        for i in inscritos
    ]
    conn.close()
    return render_template("recordatorio.html", partido=partido, links=links)


@app.route("/crear", methods=["GET", "POST"])
@organizador_required
def crear_partido():
    conn = get_db()
    if request.method == "POST":
        try:
            categoria = request.form.get("categoria", "masculino")
            if categoria not in ("masculino", "femenino", "mixto"):
                categoria = "masculino"
            conn.execute(
                "INSERT INTO partidos (organizador_id, cancha, distrito, fecha, hora, "
                "cupos_total, precio_jugador, precio_arquero, categoria, grupo_whatsapp, "
                "creado_en) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    g.usuario["id"],
                    request.form["cancha"].strip(),
                    request.form["distrito"].strip(),
                    request.form["fecha"],
                    request.form["hora"],
                    CUPOS_POR_PARTIDO,
                    float(request.form["precio_jugador"]),
                    float(request.form["precio_arquero"]),
                    categoria,
                    request.form.get("grupo_whatsapp", "").strip() or None,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            flash("Partido creado.", "ok")
            conn.close()
            return redirect(url_for("index"))
        except (ValueError, KeyError):
            flash("Revisa los datos del formulario.", "error")
    conn.close()
    return render_template("crear.html")


# ---------------------------------------------------------------------------
# Rutas web — cuenta
# ---------------------------------------------------------------------------
@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmar = request.form.get("confirmar_password", "")
        email = request.form.get("email", "").strip().lower()
        if "@" not in email or "." not in email.split("@")[-1]:
            flash("Ingresa un correo válido — lo necesitas para restablecer tu contraseña.", "error")
            return render_template("registro.html")
        if len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
            return render_template("registro.html")
        if password != confirmar:
            flash("Las contraseñas no coinciden.", "error")
            return render_template("registro.html")
        conn = get_db()
        try:
            dni = request.form.get("dni", "").strip() or None
            posicion = request.form.get("posicion", "volante")
            if posicion not in POSICIONES:
                posicion = "volante"
            nivel_juego = request.form.get("nivel_juego", "intermedio")
            if nivel_juego not in NIVELES:
                nivel_juego = "intermedio"
            quiere_organizar = request.form.get("es_organizador") == "on"
            quiere_cancha = request.form.get("es_dueno_cancha") == "on"
            cur = conn.execute(
                "INSERT INTO usuarios (nombre, telefono, email, password_hash, posicion, "
                "nivel_juego, es_organizador, organizador_solicitado, es_dueno_cancha, "
                "dni, verificado, creado_en) VALUES (?,?,?,?,?,?,0,?,?,?,0,?)",
                (
                    request.form["nombre"].strip(),
                    request.form.get("telefono", "").strip() or None,
                    email,
                    generate_password_hash(password),
                    posicion,
                    nivel_juego,
                    1 if quiere_organizar else 0,
                    1 if quiere_cancha else 0,
                    dni,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            session.clear()
            session["usuario_id"] = cur.lastrowid
            if quiere_organizar:
                flash(
                    "Cuenta creada. Tu solicitud de organizador quedó pendiente de "
                    "aprobación del equipo de MatchFutbol.", "ok",
                )
            else:
                flash("Cuenta creada. ¡Bienvenido a MatchFutbol!", "ok")
            conn.close()
            return redirect(url_for("index"))
        except sqlite3.IntegrityError:
            flash("Ese teléfono o correo ya está registrado.", "error")
        conn.close()
    return render_template("registro.html")


@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    conn = get_db()
    if request.method == "POST":
        posicion = request.form.get("posicion", g.usuario["posicion"])
        if posicion not in POSICIONES:
            posicion = g.usuario["posicion"]
        nivel_juego = request.form.get("nivel_juego", g.usuario["nivel_juego"])
        if nivel_juego not in NIVELES:
            nivel_juego = g.usuario["nivel_juego"]
        dni_nuevo = request.form.get("dni", "").strip() or None
        email_nuevo = request.form.get("email", "").strip().lower() or None
        telefono_nuevo = request.form.get("telefono", "").strip() or None
        if email_nuevo and ("@" not in email_nuevo or "." not in email_nuevo.split("@")[-1]):
            conn.close()
            flash("Ingresa un correo válido.", "error")
            return redirect(url_for("perfil"))
        try:
            if dni_nuevo and dni_nuevo != g.usuario["dni"]:
                # Si cambia el DNI (o lo agrega por primera vez), vuelve a quedar
                # pendiente de verificación.
                conn.execute(
                    "UPDATE usuarios SET posicion=?, nivel_juego=?, email=?, telefono=?, "
                    "dni=?, verificado=0 WHERE id=?",
                    (posicion, nivel_juego, email_nuevo, telefono_nuevo, dni_nuevo, g.usuario["id"]),
                )
                flash("Perfil actualizado. Tu nuevo DNI quedó pendiente de verificación.", "ok")
            else:
                conn.execute(
                    "UPDATE usuarios SET posicion=?, nivel_juego=?, email=?, telefono=? WHERE id=?",
                    (posicion, nivel_juego, email_nuevo, telefono_nuevo, g.usuario["id"]),
                )
                flash("Perfil actualizado.", "ok")
            conn.commit()
        except sqlite3.IntegrityError:
            flash("Ese correo o teléfono ya está en uso por otra cuenta.", "error")
        conn.close()
        return redirect(url_for("perfil"))

    usuario = conn.execute(
        "SELECT * FROM usuarios WHERE id = ?", (g.usuario["id"],)
    ).fetchone()
    historial = conn.execute(
        "SELECT p.id AS partido_id, p.cancha, p.distrito, p.fecha, p.hora, "
        "i.posicion, i.estado_pago "
        "FROM inscripciones i JOIN partidos p ON p.id = i.partido_id "
        "WHERE i.usuario_id = ? ORDER BY p.fecha DESC, p.hora DESC",
        (g.usuario["id"],),
    ).fetchall()
    hoy = datetime.now().strftime("%Y-%m-%d")
    jugados = [h for h in historial if h["fecha"] < hoy]
    proximos = [h for h in historial if h["fecha"] >= hoy]
    reservas = conn.execute(
        "SELECT r.*, c.nombre AS cancha_nombre, c.distrito FROM reservas r "
        "JOIN canchas c ON c.id = r.cancha_id WHERE r.usuario_id = ? "
        "ORDER BY r.fecha DESC, r.hora DESC",
        (g.usuario["id"],),
    ).fetchall()
    conn.close()
    return render_template(
        "perfil.html", usuario=usuario, jugados=jugados, proximos=proximos,
        reservas=reservas,
    )


@app.route("/notificaciones")
@login_required
def notificaciones():
    conn = get_db()
    notifs = conn.execute(
        "SELECT * FROM notificaciones WHERE usuario_id = ? ORDER BY creado_en DESC",
        (g.usuario["id"],),
    ).fetchall()
    conn.execute(
        "UPDATE notificaciones SET leida = 1 WHERE usuario_id = ? AND leida = 0",
        (g.usuario["id"],),
    )
    conn.commit()
    conn.close()
    return render_template("notificaciones.html", notificaciones=notifs)


# ---------------------------------------------------------------------------
# Panel admin — verificación de identidad de organizadores
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            flash("Sesión de admin iniciada.", "ok")
            return redirect(url_for("admin_dashboard"))
        flash("Password de admin incorrecto.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    flash("Sesión de admin cerrada.", "ok")
    return redirect(url_for("index"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    conn = get_db()
    hoy = datetime.now().strftime("%Y-%m-%d")

    def contar(sql, params=()):
        return conn.execute(sql, params).fetchone()["n"]

    metricas = {
        "partidos_activos": contar(
            "SELECT COUNT(*) AS n FROM partidos WHERE estado='activo' AND fecha >= ?", (hoy,)
        ),
        "partidos_totales": contar("SELECT COUNT(*) AS n FROM partidos"),
        "partidos_cancelados": contar("SELECT COUNT(*) AS n FROM partidos WHERE estado='cancelado'"),
        "usuarios": contar("SELECT COUNT(*) AS n FROM usuarios"),
        "organizadores_aprobados": contar("SELECT COUNT(*) AS n FROM usuarios WHERE es_organizador=1"),
        "organizadores_pendientes": contar(
            "SELECT COUNT(*) AS n FROM usuarios WHERE organizador_solicitado=1 AND es_organizador=0"
        ),
        "organizadores_verificados": contar(
            "SELECT COUNT(*) AS n FROM usuarios WHERE es_organizador=1 AND verificado=1"
        ),
        "duenos_cancha": contar("SELECT COUNT(*) AS n FROM usuarios WHERE es_dueno_cancha=1"),
        "canchas": contar("SELECT COUNT(*) AS n FROM canchas"),
        "reservas_pendientes": contar("SELECT COUNT(*) AS n FROM reservas WHERE estado='pendiente'"),
        "inscripciones_pagadas": contar("SELECT COUNT(*) AS n FROM inscripciones WHERE estado_pago='pagado'"),
        "inscripciones_pendientes": contar(
            "SELECT COUNT(*) AS n FROM inscripciones WHERE estado_pago='pendiente'"
        ),
        "equipos_liga": contar("SELECT COUNT(*) AS n FROM equipos"),
    }
    recaudado = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN i.rol='arquero' THEN p.precio_arquero ELSE p.precio_jugador "
        "END), 0) AS total FROM inscripciones i JOIN partidos p ON p.id = i.partido_id "
        "WHERE i.estado_pago = 'pagado'"
    ).fetchone()["total"]
    conn.close()
    return render_template("admin_dashboard.html", m=metricas, recaudado=recaudado)


@app.route("/admin/usuarios")
@admin_required
def admin_usuarios():
    conn = get_db()
    q = request.args.get("q", "").strip()
    if q:
        like = f"%{q}%"
        usuarios = conn.execute(
            "SELECT * FROM usuarios WHERE nombre LIKE ? OR email LIKE ? OR telefono LIKE ? "
            "ORDER BY creado_en DESC",
            (like, like, like),
        ).fetchall()
    else:
        usuarios = conn.execute(
            "SELECT * FROM usuarios ORDER BY creado_en DESC"
        ).fetchall()
    conn.close()
    return render_template("admin_usuarios.html", usuarios=usuarios, q=q)


@app.route("/admin/organizadores")
@admin_required
def admin_organizadores():
    conn = get_db()
    pendientes = conn.execute(
        "SELECT * FROM usuarios WHERE organizador_solicitado = 1 AND es_organizador = 0 "
        "ORDER BY nombre"
    ).fetchall()
    organizadores = conn.execute(
        "SELECT * FROM usuarios WHERE es_organizador = 1 ORDER BY verificado, nombre"
    ).fetchall()
    conn.close()
    return render_template(
        "admin_organizadores.html", pendientes=pendientes, organizadores=organizadores
    )


@app.route("/admin/organizadores/<int:usuario_id>/aprobar", methods=["POST"])
@admin_required
def admin_aprobar_organizador(usuario_id):
    conn = get_db()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if usuario is None:
        conn.close()
        flash("Ese usuario no existe.", "error")
        return redirect(url_for("admin_organizadores"))
    conn.execute("UPDATE usuarios SET es_organizador = 1 WHERE id = ?", (usuario_id,))
    crear_notificacion(
        conn, usuario_id, "confirmacion",
        "Tu solicitud de organizador fue aprobada. Ya puedes crear partidos.",
    )
    conn.commit()
    conn.close()
    flash(f"{usuario['nombre']} aprobado como organizador.", "ok")
    return redirect(url_for("admin_organizadores"))


@app.route("/admin/organizadores/<int:usuario_id>/verificar", methods=["POST"])
@admin_required
def admin_verificar_organizador(usuario_id):
    conn = get_db()
    usuario = conn.execute("SELECT * FROM usuarios WHERE id = ?", (usuario_id,)).fetchone()
    if usuario is None:
        conn.close()
        flash("Ese usuario no existe.", "error")
        return redirect(url_for("admin_organizadores"))
    if not usuario["dni"]:
        conn.close()
        flash("Ese organizador no ha registrado DNI todavía.", "error")
        return redirect(url_for("admin_organizadores"))
    conn.execute("UPDATE usuarios SET verificado = 1 WHERE id = ?", (usuario_id,))
    crear_notificacion(
        conn, usuario_id, "confirmacion",
        "Tu identidad fue verificada. Ahora apareces como organizador verificado.",
    )
    conn.commit()
    conn.close()
    flash(f"{usuario['nombre']} verificado.", "ok")
    return redirect(url_for("admin_organizadores"))


# ---------------------------------------------------------------------------
# Canchas (rol dueño de cancha)
# ---------------------------------------------------------------------------
@app.route("/canchas")
def canchas():
    conn = get_db()
    lista = conn.execute(
        "SELECT c.*, u.nombre AS dueno_nombre FROM canchas c "
        "JOIN usuarios u ON u.id = c.dueno_id ORDER BY c.distrito, c.nombre"
    ).fetchall()
    conn.close()
    return render_template("canchas.html", canchas=lista)


@app.route("/mis-canchas")
@dueno_cancha_required
def mis_canchas():
    conn = get_db()
    lista = conn.execute(
        "SELECT * FROM canchas WHERE dueno_id = ? ORDER BY nombre", (g.usuario["id"],)
    ).fetchall()
    reservas = conn.execute(
        "SELECT r.*, c.nombre AS cancha_nombre, u.nombre AS solicitante, "
        "u.telefono AS solicitante_telefono "
        "FROM reservas r JOIN canchas c ON c.id = r.cancha_id "
        "JOIN usuarios u ON u.id = r.usuario_id "
        "WHERE c.dueno_id = ? ORDER BY (r.estado = 'pendiente') DESC, r.fecha, r.hora",
        (g.usuario["id"],),
    ).fetchall()
    conn.close()
    return render_template("mis_canchas.html", canchas=lista, reservas=reservas)


@app.route("/canchas/nueva", methods=["GET", "POST"])
@dueno_cancha_required
def nueva_cancha():
    if request.method == "POST":
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO canchas (dueno_id, nombre, distrito, direccion, "
                "telefono_contacto, descripcion, horarios, creado_en) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    g.usuario["id"],
                    request.form["nombre"].strip(),
                    request.form["distrito"].strip(),
                    request.form.get("direccion", "").strip() or None,
                    request.form.get("telefono_contacto", "").strip() or g.usuario["telefono"],
                    request.form.get("descripcion", "").strip() or None,
                    request.form.get("horarios", "").strip() or None,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            flash("Cancha registrada.", "ok")
            conn.close()
            return redirect(url_for("mis_canchas"))
        except (ValueError, KeyError):
            flash("Revisa los datos del formulario.", "error")
        conn.close()
    return render_template("cancha_form.html", cancha=None)


@app.route("/canchas/<int:cancha_id>/editar", methods=["GET", "POST"])
@dueno_cancha_required
def editar_cancha(cancha_id):
    conn = get_db()
    cancha = conn.execute("SELECT * FROM canchas WHERE id = ?", (cancha_id,)).fetchone()
    if cancha is None or cancha["dueno_id"] != g.usuario["id"]:
        conn.close()
        flash("No se encontró esa cancha.", "error")
        return redirect(url_for("mis_canchas"))
    if request.method == "POST":
        conn.execute(
            "UPDATE canchas SET nombre=?, distrito=?, direccion=?, telefono_contacto=?, "
            "descripcion=?, horarios=? WHERE id=?",
            (
                request.form["nombre"].strip(),
                request.form["distrito"].strip(),
                request.form.get("direccion", "").strip() or None,
                request.form.get("telefono_contacto", "").strip() or g.usuario["telefono"],
                request.form.get("descripcion", "").strip() or None,
                request.form.get("horarios", "").strip() or None,
                cancha_id,
            ),
        )
        conn.commit()
        conn.close()
        flash("Cancha actualizada.", "ok")
        return redirect(url_for("mis_canchas"))
    conn.close()
    return render_template("cancha_form.html", cancha=cancha)


@app.route("/canchas/<int:cancha_id>/reservar", methods=["GET", "POST"])
@login_required
def reservar_cancha(cancha_id):
    conn = get_db()
    cancha = conn.execute(
        "SELECT c.*, u.nombre AS dueno_nombre FROM canchas c "
        "JOIN usuarios u ON u.id = c.dueno_id WHERE c.id = ?", (cancha_id,)
    ).fetchone()
    if cancha is None:
        conn.close()
        flash("Esa cancha no existe.", "error")
        return redirect(url_for("canchas"))
    if request.method == "POST":
        try:
            fecha = request.form["fecha"]
            hora = request.form["hora"]
            nota = request.form.get("nota", "").strip() or None
            cur = conn.execute(
                "INSERT INTO reservas (cancha_id, usuario_id, fecha, hora, nota, estado, "
                "creado_en) VALUES (?,?,?,?,?,'pendiente',?)",
                (cancha_id, g.usuario["id"], fecha, hora, nota,
                 datetime.now().isoformat(timespec="seconds")),
            )
            crear_notificacion(
                conn, cancha["dueno_id"], "confirmacion",
                f"{g.usuario['nombre']} pidió reservar {cancha['nombre']} el {fecha} a las "
                f"{hora}. Revisa tus canchas para confirmar o rechazar.",
            )
            conn.commit()
            reserva_id = cur.lastrowid
            conn.close()
            flash(
                "Solicitud de reserva enviada. El dueño de la cancha debe confirmarla.", "ok",
            )
            return redirect(url_for("ver_reserva", reserva_id=reserva_id))
        except (ValueError, KeyError):
            flash("Revisa los datos del formulario.", "error")
    conn.close()
    return render_template("reservar_cancha.html", cancha=cancha)


@app.route("/reservas/<int:reserva_id>")
@login_required
def ver_reserva(reserva_id):
    conn = get_db()
    reserva = conn.execute(
        "SELECT r.*, c.nombre AS cancha_nombre, c.distrito, c.dueno_id, "
        "u.nombre AS dueno_nombre, u.telefono AS dueno_telefono "
        "FROM reservas r JOIN canchas c ON c.id = r.cancha_id "
        "JOIN usuarios u ON u.id = c.dueno_id WHERE r.id = ?",
        (reserva_id,),
    ).fetchone()
    conn.close()
    if reserva is None or reserva["usuario_id"] != g.usuario["id"]:
        flash("No se encontró esa reserva.", "error")
        return redirect(url_for("canchas"))
    return render_template("ver_reserva.html", reserva=reserva)


@app.route("/reservas/<int:reserva_id>/confirmar", methods=["POST"])
@dueno_cancha_required
def confirmar_reserva(reserva_id):
    conn = get_db()
    reserva = conn.execute(
        "SELECT r.*, c.dueno_id, c.nombre AS cancha_nombre FROM reservas r "
        "JOIN canchas c ON c.id = r.cancha_id WHERE r.id = ?", (reserva_id,)
    ).fetchone()
    if reserva is None or reserva["dueno_id"] != g.usuario["id"]:
        conn.close()
        flash("No se encontró esa reserva.", "error")
        return redirect(url_for("mis_canchas"))
    conn.execute("UPDATE reservas SET estado = 'confirmada' WHERE id = ?", (reserva_id,))
    crear_notificacion(
        conn, reserva["usuario_id"], "confirmacion",
        f"Tu reserva en {reserva['cancha_nombre']} el {reserva['fecha']} a las "
        f"{reserva['hora']} fue CONFIRMADA por el dueño.",
    )
    conn.commit()
    conn.close()
    flash("Reserva confirmada.", "ok")
    return redirect(url_for("mis_canchas"))


@app.route("/reservas/<int:reserva_id>/rechazar", methods=["POST"])
@dueno_cancha_required
def rechazar_reserva(reserva_id):
    conn = get_db()
    reserva = conn.execute(
        "SELECT r.*, c.dueno_id, c.nombre AS cancha_nombre FROM reservas r "
        "JOIN canchas c ON c.id = r.cancha_id WHERE r.id = ?", (reserva_id,)
    ).fetchone()
    if reserva is None or reserva["dueno_id"] != g.usuario["id"]:
        conn.close()
        flash("No se encontró esa reserva.", "error")
        return redirect(url_for("mis_canchas"))
    conn.execute("UPDATE reservas SET estado = 'rechazada' WHERE id = ?", (reserva_id,))
    crear_notificacion(
        conn, reserva["usuario_id"], "cupo_liberado",
        f"Tu reserva en {reserva['cancha_nombre']} el {reserva['fecha']} a las "
        f"{reserva['hora']} fue rechazada por el dueño. Prueba otro horario.",
    )
    conn.commit()
    conn.close()
    flash("Reserva rechazada.", "ok")
    return redirect(url_for("mis_canchas"))


# ---------------------------------------------------------------------------
# Liga MatchFutbol — tabla de posiciones
# ---------------------------------------------------------------------------
@app.route("/liga")
def liga():
    conn = get_db()
    tabla = conn.execute(
        "SELECT e.nombre, e.distrito, l.* FROM liga_tabla l "
        "JOIN equipos e ON e.id = l.equipo_id "
        "WHERE l.temporada = ? "
        "ORDER BY l.pts DESC, (l.gf - l.gc) DESC, l.gf DESC",
        (TEMPORADA_ACTUAL,),
    ).fetchall()
    conn.close()
    return render_template("liga.html", tabla=tabla, temporada=TEMPORADA_ACTUAL)


@app.route("/admin/liga")
@admin_required
def admin_liga():
    conn = get_db()
    equipos = conn.execute("SELECT * FROM equipos ORDER BY nombre").fetchall()
    tabla_por_equipo = {
        r["equipo_id"]: r
        for r in conn.execute(
            "SELECT * FROM liga_tabla WHERE temporada = ?", (TEMPORADA_ACTUAL,)
        ).fetchall()
    }
    conn.close()
    return render_template(
        "admin_liga.html", equipos=equipos, tabla=tabla_por_equipo, temporada=TEMPORADA_ACTUAL
    )


@app.route("/admin/liga/equipos", methods=["POST"])
@admin_required
def admin_liga_nuevo_equipo():
    conn = get_db()
    try:
        nombre = request.form["nombre"].strip()
        distrito = request.form["distrito"].strip()
        conn.execute(
            "INSERT INTO equipos (nombre, distrito, creado_en) VALUES (?,?,?)",
            (nombre, distrito, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        flash(f"Equipo {nombre} agregado a la liga.", "ok")
    except sqlite3.IntegrityError:
        flash("Ya existe un equipo con ese nombre.", "error")
    except (ValueError, KeyError):
        flash("Revisa los datos del formulario.", "error")
    conn.close()
    return redirect(url_for("admin_liga"))


@app.route("/admin/liga/<int:equipo_id>/actualizar", methods=["POST"])
@admin_required
def admin_liga_actualizar(equipo_id):
    conn = get_db()
    try:
        pj = int(request.form.get("pj", 0))
        pg = int(request.form.get("pg", 0))
        pe = int(request.form.get("pe", 0))
        pp = int(request.form.get("pp", 0))
        gf = int(request.form.get("gf", 0))
        gc = int(request.form.get("gc", 0))
        pts = pg * 3 + pe
        ahora = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO liga_tabla (equipo_id, temporada, pj, pg, pe, pp, gf, gc, pts, "
            "actualizado_en) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(equipo_id, temporada) DO UPDATE SET "
            "pj=excluded.pj, pg=excluded.pg, pe=excluded.pe, pp=excluded.pp, "
            "gf=excluded.gf, gc=excluded.gc, pts=excluded.pts, actualizado_en=excluded.actualizado_en",
            (equipo_id, TEMPORADA_ACTUAL, pj, pg, pe, pp, gf, gc, pts, ahora),
        )
        conn.commit()
        flash("Tabla de posiciones actualizada.", "ok")
    except (ValueError, KeyError):
        flash("Revisa los datos del formulario.", "error")
    conn.close()
    return redirect(url_for("admin_liga"))


# ---------------------------------------------------------------------------
# API JSON (para verificar rapido o integrar despues)
# ---------------------------------------------------------------------------
@app.route("/api/partidos")
def api_partidos():
    conn = get_db()
    ids = conn.execute("SELECT id FROM partidos ORDER BY fecha, hora").fetchall()
    data = [partido_con_cupos(conn, r["id"]) for r in ids]
    conn.close()
    return jsonify(data)


# init_db() se llama siempre al importar el modulo (no solo bajo
# "python app.py"), porque un servidor WSGI de produccion como gunicorn
# importa `app` directamente y nunca ejecuta el bloque __main__ de abajo.
# Sin esto, la base de datos nunca se crearia en un despliegue real.
print(f"[MatchFutbol] DB_PATH = {DB_PATH!r}", flush=True)
print(f"[MatchFutbol] dirname existe = {os.path.isdir(os.path.dirname(DB_PATH) or '.')}", flush=True)
try:
    print(f"[MatchFutbol] contenido de {os.path.dirname(DB_PATH) or '.'} = "
          f"{os.listdir(os.path.dirname(DB_PATH) or '.')}", flush=True)
except Exception as _e:
    print(f"[MatchFutbol] no se pudo listar el directorio: {_e!r}", flush=True)
init_db()

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    print(f"MatchFutbol MVP corriendo en http://localhost:{puerto}")
    app.run(host="0.0.0.0", port=puerto, debug=False)
