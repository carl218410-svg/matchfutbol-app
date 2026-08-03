# MatchFutbol — MVP

Demo funcional de la app MatchFutbol (Huacho / Norte Chico). Es una
versión mínima para probar el concepto descrito en
`../DISENO_MATCHFUTBOL.md`: encontrar partidos, inscribirse (jugador o
arquero, con precio diferenciado) y crear partidos como organizador.

## Qué incluye

- **Listar partidos** disponibles con cupos en tiempo real, con posición
  tipo Trova (arquero / defensa / volante / delantero).
- **Ver un partido** e inscribirse eligiendo posición y precio diferenciado
  para arquero.
- **Crear partido** (rol organizador, aprobado por admin).
- **Registrar usuario** (con correo) e **iniciar sesión** con teléfono +
  contraseña (sesiones Flask).
- **Restablecer contraseña por correo:** en `/recuperar` (modo demo: el
  enlace de restablecimiento se muestra en pantalla en vez de enviarse por
  email real, porque no hay credenciales SMTP configuradas).
- **Editar y cancelar partido**, con notificación a los inscritos si
  cambia cancha/fecha/hora, o aviso de cancelación con enlaces de WhatsApp.
- **Filtros de distrito y fecha** en la lista de partidos.
- **Reserva directa de cancha** (sin pasar por un partido armado): el
  jugador pide un horario, el dueño lo confirma o rechaza.
- **Panel de métricas para admin** (`/admin`): partidos, usuarios, pagos,
  canchas y liga de un vistazo.
- **Pago dentro de la app (simulado):** al inscribirte quedas en estado
  "pendiente" hasta registrar el pago (Yape con código de operación, o
  efectivo en cancha).
- **Notificaciones + recordatorio real por WhatsApp:** confirmaciones y
  avisos dentro de la app, más enlaces `wa.me` (mensaje pre-escrito) para
  que el organizador le avise a cada inscrito directo a su celular.
- **Verificación de identidad de organizadores:** DNI opcional en registro
  o perfil, y panel admin para aprobar solicitudes de organizador y
  verificar identidad.
- **Perfil de usuario:** editar posición/nivel de juego, agregar DNI,
  ver historial de partidos jugados y próximos con estado de pago.
- **Rol Dueño de cancha:** autodeclarado en el registro; gestiona la ficha
  de su(s) cancha(s) (horarios, contacto, descripción) en `/mis-canchas`;
  directorio público en `/canchas` con enlace directo a WhatsApp.
- **Liga MatchFutbol:** tabla de posiciones simple por temporada en
  `/liga`, editable por el admin en `/admin/liga` (puntos se calculan
  solos).
- Base de datos SQLite que se crea sola con datos de ejemplo de Huacho.

## Cuentas de prueba

Todos los usuarios sembrados usan la contraseña `huacho123` (teléfono como
usuario, correo `nombre.apellido@matchfutbol.demo` en minúsculas — útil para
probar `/recuperar`). Algunos casos útiles para probar:

- `999111222` (Carlos Rojas) — organizador aprobado y verificado.
- `999222333` (Lucía Fernández) — organizadora aprobada, identidad pendiente.
- `999222888` (Sofía Meza) — solicitud de organizador pendiente de aprobación.
- `999888111` (Pedro Salas) — dueño de cancha, ya tiene 2 canchas cargadas.
- `999555666` (Luis Campos) — jugador común, sin roles especiales.

Panel admin: `/admin/login`, password `admin123` (o variable de entorno
`MATCHFUTBOL_ADMIN_PASSWORD`). Desde ahí: aprobar organizadores, verificar
DNI, y gestionar la tabla de la liga (`/admin/liga`).

## Cómo ejecutar

```bash
cd mvp
pip install -r requirements.txt
python app.py
```

Luego abre http://localhost:5000 en el navegador.

La primera vez se crea `matchfutbol.db` con datos de ejemplo. Si
quieres empezar de cero, borra ese archivo y vuelve a ejecutar.

## Desplegar en producción

El diseño original sugiere un hosting económico de bajo costo fijo tipo
Railway, Render o Fly.io para el piloto — el proyecto ya está listo para
cualquiera de esos tres:

1. **Variables de entorno.** Copia `.env.example` a `.env` (o cárgalas
   directamente en el panel de tu proveedor) y **cambia los valores por
   defecto** antes de lanzar:
   - `MATCHFUTBOL_SECRET_KEY` — clave para firmar las cookies de sesión.
     Genera una con `python -c "import secrets; print(secrets.token_hex(32))"`.
   - `MATCHFUTBOL_ADMIN_PASSWORD` — password del panel `/admin/login`
     (por defecto es `admin123`, cualquiera podría entrar si no lo cambias).
2. **Servidor de producción.** El repo incluye un `Procfile`
   (`web: gunicorn app:app`) y `gunicorn` ya está en `requirements.txt` —
   no uses el servidor de desarrollo de Flask (`python app.py`) en
   producción. La mayoría de estos proveedores detecta el `Procfile`
   automáticamente; si tu plataforma pide un comando de arranque manual,
   usa `gunicorn app:app`.
3. **Puerto.** La app lee la variable `PORT` que estos proveedores
   inyectan automáticamente (con `python app.py` sigue usando el 5000
   por defecto si `PORT` no está definida).
4. **Base de datos.** Se sigue usando SQLite (`matchfutbol.db`), que se
   crea sola la primera vez que arranca. Ojo: en la mayoría de estos
   hostings el sistema de archivos **no es persistente** entre
   despliegues a menos que uses un volumen/disco persistente — revisa la
   documentación de tu proveedor y monta un volumen para `matchfutbol.db`,
   o migra a PostgreSQL cuando el piloto crezca (tal como recomienda la
   sección 4 del documento de diseño).

## Estructura

```
mvp/
├── app.py               # backend Flask + SQLite (modelos, rutas, seed)
├── requirements.txt     # flask
├── templates/
│   ├── base.html               # layout, estilos y nav
│   ├── index.html              # lista de partidos
│   ├── partido.html            # detalle + inscripción
│   ├── crear.html              # crear partido
│   ├── registro.html           # registro de usuario
│   ├── login.html               # inicio de sesión
│   ├── perfil.html              # perfil + historial de partidos
│   ├── pago.html                 # confirmar pago de una inscripción
│   ├── notificaciones.html       # bandeja de notificaciones
│   ├── recordatorio.html         # enlaces wa.me para avisar a inscritos
│   ├── canchas.html              # directorio público de canchas
│   ├── mis_canchas.html          # canchas del dueño logueado
│   ├── cancha_form.html          # crear/editar ficha de cancha
│   ├── liga.html                 # tabla de posiciones pública
│   ├── admin_login.html          # login del panel admin
│   ├── admin_organizadores.html  # aprobar organizadores + verificar DNI
│   └── admin_liga.html           # editar tabla de la liga
└── matchfutbol.db       # (se genera al ejecutar)
```

## Endpoint de verificación

`GET /api/partidos` devuelve la lista de partidos con cupos en JSON —
útil para probar rápido que el backend responde.

## Qué NO incluye este MVP (a propósito)

El restablecimiento de contraseña es simulado: genera un enlace real y
funcional, pero lo muestra en pantalla en vez de mandarlo por correo (no
hay credenciales SMTP/SendGrid configuradas). Para producción, conectar
`smtplib` o un proveedor como SendGrid/Mailgun con esas credenciales.
El pago dentro de la app es un registro manual (Yape con código de
operación o efectivo), no una pasarela real integrada. El recordatorio
que "llega al celular" es un enlace `wa.me` que el organizador debe
abrir y enviar manualmente — no hay envío automático (eso requeriría una
cuenta de Twilio/WhatsApp Business API de pago). La verificación de
identidad valida que se ingresó un DNI, pero no lo contrasta contra
RENIEC ni ningún servicio externo. La tabla de la Liga se actualiza a
mano desde el panel admin (no hay carga automática de resultados desde
los partidos creados en la app — son conceptos separados). Reserva
directa de cancha integrada a la creación de partidos y pasarela de pago
real siguen fuera de alcance. Todo esto queda para siguientes
iteraciones antes de un lanzamiento real.
