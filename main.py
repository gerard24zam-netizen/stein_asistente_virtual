import os
import json
import re
import requests
import datetime
import pytz
import threading
import uuid
import resend
import calendar
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from supabase import create_client
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer as Serializer


app = Flask(__name__)

# --- CONFIGURACIÓN ---
TELEFONO_ID_META = "1120833397777315"
META_TOKEN = "EAAXdEhil3gMBR0uiujuuAvK5nqaj8A9boQQ7Yd59u0Xa8GF86XVtJl2k7EWLecDPk74CCtBbu0VH2cOIL8DW9zd4h3Mbv3sdbmReK473770t9TDfyDZCqJhomFBbxc0kSu5zgpZAy4cWMNnssZAyZB81Gb6c9dfmwfrzTYGjy6oOIc7d7Px8vTATQ9cwHKROmwZDZD"
VERIFY_TOKEN = "TOKEN_SECRETO_META"
SCOPES = ['https://www.googleapis.com/auth/calendar']

app.secret_key = 'tu_clave_secreta'
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
resend.api_key = os.environ.get("RESEND_API_KEY")

# Permitir HTTP local para pruebas si es necesario (quitar en producción estricta con HTTPS)
# os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

def get_serializer():
    return Serializer(app.secret_key)

def log(msg):
    print(f"DEBUG: {msg}", flush=True)

def obtener_servicio_calendar_por_doctor(calendar_id):
    try:
        res = supabase.table("Doctores").select("google_token_json").eq("calendar_id", calendar_id).execute()
        if res.data and res.data[0].get("google_token_json"):
            token_info = json.loads(res.data[0].get("google_token_json"))
            creds = Credentials.from_authorized_user_info(token_info, SCOPES)
            return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        log(f"Error cargando calendario OAuth para {calendar_id}: {e}")
    return None

def limpiar_telefono(tel):
    return "".join(filter(str.isdigit, str(tel)))[-10:]

def extraer_nombre_limpio(titulo):
    titulo_limpio = titulo.replace(' ✅', '').replace(' ❌', '').replace('✅', '').replace('❌', '').strip()
    palabras = [p for p in titulo_limpio.split() if not p.isdigit()]
    nombre = " ".join(palabras).strip()
    return nombre if nombre else "Paciente"

def enviar_mensaje(telefono, tipo, contenido=None, template_params=None, template_name="confirmacion_cita"):
    headers = {"Authorization": f"Bearer {META_TOKEN}", "Content-Type": "application/json"}
    url = f"https://graph.facebook.com/v17.0/{TELEFONO_ID_META}/messages"
    
    if tipo == "template":
        payload = {
            "messaging_product": "whatsapp", "to": telefono, "type": "template",
            "template": {
                "name": template_name, "language": {"code": "es_MX"},
                "components": [{"type": "body", "parameters": template_params}]
            }
        }
    else:
        payload = {"messaging_product": "whatsapp", "to": telefono, "text": {"body": contenido}}
        
    try:
        return requests.post(url, json=payload, headers=headers)
    except Exception as e:
        log(f"Error enviando mensaje: {e}")
        return None

def buscar_doctor_por_telefono(telefono_recibido):
    tel_buscado = limpiar_telefono(telefono_recibido)
    if not supabase:
        return None
    try:
        response = supabase.table("Doctores").select("*").execute()
        for doc in (response.data or []):
            wa_link = doc.get("wa_link") or doc.get("link") or ""
            if tel_buscado in limpiar_telefono(wa_link):
                return doc
    except Exception as e:
        log(f"Error buscando doctor por teléfono: {e}")
    return None

@app.route('/enviar-recordatorios-hora', methods=['GET', 'POST'])
def enviar_recordatorios_hora():
    try:
        zona_mexico = pytz.timezone('America/Mexico_City')
        ahora = datetime.now(zona_mexico)
        
        response = supabase.table("Doctores").select("*").execute()
        doctores = response.data if response.data else []

        citas_notificadas = 0
        inicio = ahora.replace(hour=0, minute=0, second=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
        fin = ahora.replace(hour=23, minute=59, second=59).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')

        for doc in doctores:
            cal_id = doc.get("calendar_id") or doc.get("email")
            if not cal_id:
                continue
            
            calendario = obtener_servicio_calendar_por_doctor(cal_id)
            if not calendario:
                continue
            
            try:
                eventos = calendario.events().list(calendarId=cal_id, timeMin=inicio, timeMax=fin, singleEvents=True).execute().get('items', [])
            except Exception:
                continue

            for evento in eventos:
                start_dt = evento.get('start', {}).get('dateTime')
                if not start_dt:
                    continue
                
                dt_cita = datetime.fromisoformat(start_dt).astimezone(zona_mexico)
                diferencia_minutos = (dt_cita - ahora).total_seconds() / 60

                if 50 <= diferencia_minutos <= 70:
                    titulo = evento.get('summary', '')
                    if "✅" in titulo or "❌" in titulo or "cancelado" in titulo.lower() or "⏰" in titulo:
                        continue
                    
                    texto = f"{titulo} {evento.get('description', '')}"
                    digitos = "".join(filter(str.isdigit, texto))
                    if len(digitos) < 10:
                        continue
                    
                    telefono = "52" + digitos[-10:]
                    nombre_paciente = extraer_nombre_limpio(titulo)
                    nombre_profesional = doc.get("name") or doc.get("nombre") or "doctor"

                    mensaje = (
                        f"Hola {nombre_paciente}, recordatorio, prepárate para tu cita con "
                        f"{nombre_profesional} empieza en una hora. Recuerda llegar a tiempo "
                        f"y llevar el total de tu consulta.\n\n"
                        f"*Stein A. V. P.*"
                    )

                    exito = enviar_mensaje(telefono, "text", contenido=mensaje)
                    if exito and exito.status_code < 400:
                        try:
                            supabase.table('metricas_y_registros').insert({
                                'calendar_id': cal_id,
                                'estado_accion': 'mensaje_enviado',
                                'confirmado': False
                            }).execute()
                        except Exception as e:
                            log(f"Error guardando métrica de recordatorio de 1h: {e}")

                        citas_notificadas += 1

        return jsonify({
            "status": "success",
            "mensaje": f"Recordatorios de 1 hora enviados: {citas_notificadas}"
        }), 200

    except Exception as e:
        print(f"Error al procesar recordatorios de 1 hora: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
        
# --- RUTAS DE GOOGLE OAUTH ---
@app.route('/authorize')
def authorize():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for('oauth2callback', _external=True)]
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    
    session['oauth_state'] = state
    session['code_verifier'] = flow.code_verifier  # <--- Guarda esto en la sesión
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    state = session.get('oauth_state')
    code_verifier = session.get('code_verifier')
    
    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for('oauth2callback', _external=True)]
        }
    }
    
    flow = Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    
    flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=request.url)
    
    creds = flow.credentials
    user_id = session.get('user_id')  # El ID personalizado del usuario en sesión
    
    try:
        # Guardamos el JSON de las credenciales en la columna google_token_json filtrando por su id
        supabase.table("Doctores").update({
            "google_token_json": creds.to_json()
        }).eq("id", user_id).execute()
    except Exception as e:
        print(f"Error al guardar el token en Supabase: {e}")
        
    return redirect(url_for('dashboard'))
    
@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip().lower()
        nombre = request.form.get('nombre', '').strip()
        ocupacion = request.form.get('ocupacion', '').strip()
        telefono = request.form.get('telefono', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        
        # Validación de términos y condiciones
        terminos = request.form.get('terminos')
        if not terminos:
            error = "Debes aceptar los términos y condiciones para continuar."
            return render_template('register.html', error=error)
        
        try:
            existing_id = supabase.table('Doctores').select('*').eq('id', usuario).execute()
            if existing_id.data and len(existing_id.data) > 0:
                error = "El nombre de usuario ya está en uso. Por favor elige otro."
                return render_template('register.html', error=error)
            
            existing_email = supabase.table('Doctores').select('*').eq('calendar_id', email).execute()
            if existing_email.data and len(existing_email.data) > 0:
                error = "Este correo electrónico ya se encuentra registrado en el sistema."
                return render_template('register.html', error=error)
            
            telefono_limpio = re.sub(r'\D', '', telefono)
            if len(telefono_limpio) == 10:
                telefono_limpio = '52' + telefono_limpio
            wa_link = f"https://wa.me/{telefono_limpio}"
            
            hashed_password = generate_password_hash(password)
            
            supabase.table('Doctores').insert({
                'id': usuario,
                'calendar_id': email,
                'name': nombre,
                'password_hash': hashed_password,
                'ocupation': ocupacion,
                'wa_link': wa_link
            }).execute()
            
            # Autenticación automática temporal en sesión y redirección al onboarding de Google Calendar
            session['user_id'] = usuario
            session['calendar_id'] = email
            
            return redirect(url_for('onboarding'))
            
        except Exception as e:
            print(f"Error al registrar usuario: {e}")
            error = "Ocurrió un error interno al procesar el registro."
            
    return render_template('register.html', error=error)

@app.route('/onboarding')
def onboarding():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('onboarding.html')

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    error = None
    success = None
    
    if request.method == 'POST':
        user_id = request.form.get('username')
        calendar_id = request.form.get('calendar_id')
        
        try:
            res = supabase.table('Doctores').select('*').eq('id', user_id).eq('calendar_id', calendar_id).execute()
            
            if res.data:
                s = get_serializer()
                token = s.dumps(user_id, salt='password-reset-salt')
                reset_url = url_for('reset_with_token', token=token, _external=True)
                
                params = {
                    "from": "Stein Asistente Virtual <onboarding@resend.dev>",
                    "to": [calendar_id],
                    "subject": "Recuperación de Contraseña",
                    "html": (
                        '<div style="font-family: Arial, sans-serif; padding: 20px; max-width: 600px; margin: auto; border: 1px solid #e2e8f0; border-radius: 10px;">'
                        '<h3 style="color: #0d6efd; text-align: center;">Recuperación de Contraseña</h3>'
                        '<p>Hola, has solicitado restablecer tu contraseña en <b>Stein Asistente Virtual</b>.</p>'
                        '<p>Haz clic en el siguiente botón para cambiarla (expira en 15 minutos):</p>'
                        '<div style="text-align: center; margin: 30px 0;">'
                        f'<a href="{reset_url}" style="background-color: #0d6efd; color: white; padding: 12px 25px; text-decoration: none; border-radius: 50px; font-weight: bold; display: inline-block;">Restablecer Contraseña</a>'
                        '</div>'
                        '<p style="color: #6c757d; font-size: 12px; text-align: center;">Si tú no lo solicitaste, ignora este mensaje.</p>'
                        '</div>'
                    )
                }
                
                resend.Emails.send(params)
                success = "Se ha enviado un enlace de recuperación a tu correo electrónico."
            else:
                error = "El usuario o el correo no coinciden con nuestros registros."
        except Exception as e:
            error = f"Error al enviar el correo: {e}"
            
    return render_template('forgot_password.html', error=error, success=success)
    
@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_with_token(token):
    s = get_serializer()
    try:
        user_id = s.loads(token, salt='password-reset-salt', max_age=900)
    except Exception:
        return "El enlace de recuperación es inválido o ya ha expirado.", 400
        
    error = None
    success = None
    
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        try:
            new_hash = generate_password_hash(new_password)
            supabase.table('Doctores').update({'password_hash': new_hash}).eq('id', user_id).execute()
            success = "¡Contraseña actualizada con éxito! Ya puedes iniciar sesión."
        except Exception as e:
            error = f"Error al actualizar: {e}"
            
    return render_template('reset_token.html', error=error, success=success)
@app.route('/')
def index():
    if 'user_id' not in session and 'usuario_web' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        login_input = request.form.get('username')
        password = request.form.get('password')
        
        try:
            user_data = None
            
            res_id = supabase.table('Doctores').select('*').eq('id', login_input).execute()
            if res_id.data and len(res_id.data) > 0:
                user_data = res_id.data[0]
            else:
                res_email = supabase.table('Doctores').select('*').eq('calendar_id', login_input).execute()
                if res_email.data and len(res_email.data) > 0:
                    user_data = res_email.data[0]
            
            if user_data and check_password_hash(user_data['password_hash'], password):
                session['user_id'] = user_data['id']
                session['calendar_id'] = user_data['calendar_id']
                return redirect(url_for('index'))
            else:
                error = "Usuario/Correo o contraseña incorrectos."
        except Exception as e:
            print(f"Error en login: {e}")
            error = "Ocurrió un error al procesar el inicio de sesión."
            
    return render_template('login.html', error=error)

@app.route('/dashboard')
def dashboard():
    if 'usuario_web' not in session and 'user_id' not in session:
        return redirect(url_for('login'))
    print("--- ¡ENTRÓ A LA RUTA DEL DASHBOARD! ---", flush=True)
    
    doctor_actual = session.get('user_id') or session.get('usuario_web')
    calendar_id = session.get('calendar_id')
    
    user_data = {}
    enviadas_global = 0
    enviadas_mes = 0
    confirmadas_hoy = 0
    canceladas_hoy = 0
    promedio = 0.0
    cantidad_encuestas = 0
    total_mensajes_facturables = 0
    costo_base_meta = 0.0
    precio_final_cliente = 0.0

    hoy_str = date.today().isoformat()  # Fecha actual en formato "YYYY-MM-DD"

    try:
        # 1. Obtener todos los datos del doctor (incluyendo created_at y campos de perfil)
        res_doc = supabase.table('Doctores').select('*').eq('id', doctor_actual).execute()
        if not res_doc.data:
            res_doc = supabase.table('Doctores').select('*').eq('calendar_id', calendar_id).execute()
        
        user_data = res_doc.data[0] if res_doc.data else {}
        created_str = user_data.get('created_at')

        if created_str:
            created_dt = datetime.fromisoformat(created_str.replace('Z', '+00:00')).date()
            reg_day = created_dt.day
            
            today = date.today()
            try:
                inicio_ciclo = date(today.year, today.month, reg_day)
            except ValueError:
                last_day = calendar.monthrange(today.year, today.month)[1]
                inicio_ciclo = date(today.year, today.month, min(reg_day, last_day))
            
            if inicio_ciclo > today:
                if today.month == 1:
                    prev_year, prev_month = today.year - 1, 12
                else:
                    prev_year, prev_month = today.year, today.month - 1
                last_day_prev = calendar.monthrange(prev_year, prev_month)[1]
                inicio_ciclo = date(prev_year, prev_month, min(reg_day, last_day_prev))
            
            inicio_mes_str = inicio_ciclo.isoformat()
        else:
            # Fallback al primer día del mes actual si no se encuentra la fecha de registro
            inicio_mes_str = date.today().replace(day=1).isoformat()

        # 2. Consultar la tabla unificada metricas_y_registros en Supabase
        response_uso = supabase.table('metricas_y_registros').select('*').eq('calendar_id', calendar_id).execute()
        
        if response_uso.data:
            registros = response_uso.data
            enviadas_global = len(registros)
            
            print(f"--- REGISTROS EN METRICAS_Y_REGISTROS para {calendar_id} ---", flush=True)
            for r in registros:
                print(f"BD Registro -> Creado: '{r.get('created_at')}' | Estado/Acción: '{r.get('estado_accion')}'", flush=True)
            print(f"Hoy string buscado: '{hoy_str}'", flush=True)

            # Filtrar registros del ciclo mensual actual
            registros_ciclo = [r for r in registros if str(r.get('created_at', '')) >= inicio_mes_str]
            enviadas_mes = len(registros_ciclo)

            # Filtrar registros del día de hoy
            registros_hoy = [r for r in registros if str(r.get('created_at', '')).startswith(hoy_str)]
            print(f"Registros coincidentes para hoy: {len(registros_hoy)}", flush=True)
            
            confirmadas_hoy = sum(1 for r in registros_hoy if r.get('estado_accion') == 'cita_confirmada')
            canceladas_hoy = sum(1 for r in registros_hoy if r.get('estado_accion') in ['cita_cancelada', 'cita_reagendada'])

            # 3. Calcular encuestas y satisfacción dentro del ciclo mensual
            encuestas_ciclo = [r for r in registros_ciclo if r.get('estado_accion') == 'encuesta_calificacion' and r.get('calificacion') is not None]
            cantidad_encuestas = len(encuestas_ciclo)
            if cantidad_encuestas > 0:
                total_cal = sum(float(r['calificacion']) for r in encuestas_ciclo)
                promedio = round(total_cal / cantidad_encuestas, 1)

           # 4. Cálculo de consumo de Meta y Modelo de Precios por Niveles (Tiers)
            total_mensajes_facturables = sum(1 for r in registros_ciclo if r.get('estado_accion') == 'mensaje_enviado')
            
            # El plan lo define el usuario manualmente en su configuración guardada
            plan_seleccionado = user.get('plan', 'comisionista')
            
            if plan_seleccionado == 'comisionista':
                nombre_plan = "Comisionista"
                precio_final = total_mensajes_facturables * 15.0
            elif plan_seleccionado == 'profesional':
                nombre_plan = "Profesional"
                precio_final = 350.0  # O la cuota fija establecida
            elif plan_seleccionado == 'v_plus':
                nombre_plan = "V. Plus"
                precio_final = 500.0  # O la cuota fija establecida
            else:
                nombre_plan = "Comisionista"
                precio_final = total_mensajes_facturables * 15.0

    except Exception as e:
        import traceback
        print(f"Error crítico en dashboard: {e}", flush=True)
        traceback.print_exc()

    metricas = {
        "citas_enviadas_global": enviadas_global,
        "citas_enviadas_mes": enviadas_mes,
        "citas_confirmadas": confirmadas_hoy,
        "citas_canceladas": canceladas_hoy,
        "promedio_satisfaccion": f"{promedio} / 10",
        "total_encuestas": cantidad_encuestas,
        "mensajes_facturables": total_mensajes_facturables,
        "nombre_plan": nombre_plan,
        "precio_sugerido": round(precio_final_cliente, 2)
    }

    print("--- DEPURACIÓN DASHBOARD ---", flush=True)
    print("Usuario en sesión:", session.get('user_id'), flush=True)
    print("Diccionario 'metricas' generado:", metricas, flush=True)
    
    return render_template('dashboard.html', user=user_data, datos=metricas)

# 1. Agrega esta nueva ruta en tu main.py para guardar el plan que el doctor elija en el selector
@app.route('/actualizar-plan', methods=['POST'])
def actualizar_plan():
    if 'usuario_web' not in session and 'user_id' not in session:
        return jsonify({"status": "error", "message": "No autorizado"}), 401
    
    doctor_actual = session.get('user_id') or session.get('usuario_web')
    calendar_id = session.get('calendar_id')
    
    nuevo_plan = request.form.get('plan') # 'pay_per_use', 'estandar', 'alto'
    
    try:
        # Actualizar en la tabla Doctores de Supabase
        supabase.table('Doctores').update({'plan_seleccionado': nuevo_plan}).eq('id', doctor_actual).execute()
        return jsonify({"status": "success", "mensaje": "Plan actualizado correctamente"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    
@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    error = None
    success = None
    
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        user_id = session['user_id']
        
        try:
            res = supabase.table('Doctores').select('password_hash').eq('id', user_id).execute()
            if res.data:
                stored_hash = res.data[0].get('password_hash')
                
                if stored_hash and check_password_hash(stored_hash, current_password):
                    new_hash = generate_password_hash(new_password)
                    supabase.table('Doctores').update({'password_hash': new_hash}).eq('id', user_id).execute()
                    success = "¡Contraseña actualizada con éxito!"
                else:
                    error = "La contraseña actual es incorrecta."
            else:
                error = "Usuario no encontrado."
        except Exception as e:
            error = f"Ocurrió un error al actualizar: {e}"
            
    return render_template('change_password.html', error=error, success=success)

@app.route('/logout')
def logout():
    session.pop('usuario_web', None)
    session.pop('user_id', None)
    session.pop('calendar_id', None)
    return redirect(url_for('login'))

@app.route('/toggle-encuestas', methods=['POST'])
def toggle_encuestas():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_id = session['user_id']
    
    try:
        res = supabase.table('Doctores').select('enviar_encuesta').eq('id', user_id).execute()
        current_status = res.data[0].get('enviar_encuesta', False) if res.data else False
        
        new_status = not current_status
        
        supabase.table('Doctores').update({'enviar_encuesta': new_status}).eq('id', user_id).execute()
        
    except Exception as e:
        print(f"Error al cambiar estado de encuestas: {e}")
        
    return redirect(url_for('index'))

@app.route('/status')
def home():
    return "API Activa", 200

@app.route('/ejecutar-proceso-diario', methods=['POST'])
def procesar_desde_supabase():
    try:
        response = supabase.table("Doctores").select("*").execute()
        doctores = response.data if response.data else []
    except Exception as e:
        log(f"Error consultando Supabase: {e}")
        return jsonify({"error": str(e)}), 500

    zona_mexico = pytz.timezone('America/Mexico_City')
    ahora = datetime.now(zona_mexico)
    fecha_hoy = str(ahora.date())

    nombres_dias = {
        "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miercoles",
        "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sabado", "Sunday": "Domingo"
    }
    dia_actual_espanol = nombres_dias[ahora.strftime("%A")]

    inicio = ahora.replace(hour=0, minute=0, second=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
    fin = ahora.replace(hour=23, minute=59, second=59).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')

    total_enviados = 0

    for doc in doctores:
        doc_nombre = doc.get("name") or doc.get("nombre") or "Dr. Gerardo"
        cal_id = doc.get("calendar_id") or doc.get("email")
        if not cal_id:
            continue

        if supabase:
            try:
                supabase.table("metricas_y_registros").insert({
                    "calendar_id": cal_id,
                    "estado_accion": "proceso_diario_iniciado"
                }).execute()
            except Exception as ex:
                log(f"Error guardando registro en metricas_y_registros: {ex}")

        calendario = obtener_servicio_calendar_por_doctor(cal_id)
        if not calendario:
            log(f"No se pudo inicializar calendario para {cal_id}")
            continue

        dias_configurados = doc.get("dias_trabajo") or "Lunes,Martes,Miercoles,Jueves,Viernes"
        trabajar_fechas_str = doc.get("trabajar_fecha") or ""
        
        es_dia_laboral_normal = dia_actual_espanol in dias_configurados
        es_fecha_excepcion = fecha_hoy in [f.strip() for f in trabajar_fechas_str.split(",") if f.strip()]

        if not es_dia_laboral_normal and not es_fecha_excepcion:
            continue

        pausa_hasta = doc.get("pausa_hasta")
        if pausa_hasta and fecha_hoy <= pausa_hasta:
            continue

        doc_ocupacion = doc.get("ocupation") or "Atención Psicológica"
        wa_link = doc.get("wa_link") or doc.get("link") or ""
        tel_doc = "".join(filter(str.isdigit, str(wa_link)))

        if es_fecha_excepcion and not es_dia_laboral_normal:
            fechas_pendientes = [f.strip() for f in trabajar_fechas_str.split(",") if f.strip() and f.strip() != fecha_hoy]
            nueva_fechas_str = ",".join(fechas_pendientes) if fechas_pendientes else None
            try:
                supabase.table("Doctores").update({"trabajar_fecha": nueva_fechas_str}).eq("calendar_id", cal_id).execute()
            except Exception as ex:
                log(f"Error limpiando trabajar_fecha en Supabase: {ex}")

        try:
            eventos = calendario.events().list(calendarId=cal_id, timeMin=inicio, timeMax=fin, singleEvents=True).execute().get('items', [])
        except Exception as e:
            log(f"Error leyendo calendario {cal_id}: {e}")
            continue

        jornada_registrada = doc.get("jornada_fecha")
        if tel_doc and jornada_registrada != fecha_hoy:
            total_citas_doc = len(eventos)
            params_jornada_doc = [
                {"type": "text", "text": doc_nombre},
                {"type": "text", "text": str(total_citas_doc)}
            ]
            resp_doc = enviar_mensaje(tel_doc, "template", template_params=params_jornada_doc, template_name="jornada_doc")
            if resp_doc and resp_doc.status_code < 400:
                try:
                    supabase.table("Doctores").update({"jornada_fecha": fecha_hoy}).eq("calendar_id", cal_id).execute()
                except Exception as ex:
                    log(f"No se pudo guardar jornada_fecha en Supabase: {ex}")

        ayer_str = str((ahora - timedelta(days=1)).date())
        promedio_str = "Sin calificaciones aún"
        total_respuestas = 0
        
        if supabase:
            try:
                res_metricas = supabase.table("metricas_y_registros").select("*").eq("calendar_id", cal_id).eq("estado_accion", "encuesta_calificacion").execute()
                registros = res_metricas.data or []
                calificaciones_ayer = [
                    r["calificacion"] for r in registros 
                    if r.get("fecha", "").startswith(ayer_str) and r.get("calificacion") is not None
                ]
                if calificaciones_ayer:
                    total_respuestas = len(calificaciones_ayer)
                    promedio = sum(calificaciones_ayer) / total_respuestas
                    promedio_str = f"{promedio:.1f} / 10"
            except Exception as ex:
                log(f"Error calculando promedio de encuestas desde metricas_y_registros: {ex}")

        if tel_doc and total_respuestas > 0:
            mensaje_balance = (
                f"📊 *Reporte de Satisfacción de Ayer*:\n"
                f"Recibiste {total_respuestas} respuesta(s).\n"
                f"Calificación promedio: *{promedio_str}*.\n"
                f"¡Excelente trabajo Dr. {doc_nombre}! *Stein tu Asistente Virtual*"
            )
            enviar_mensaje(tel_doc, "text", contenido=mensaje_balance)

        for evento in eventos:
            titulo = evento.get('summary', '')
            descripcion = evento.get('description', '')
            
            if "✅" in titulo or "❌" in titulo:
                continue

            texto = f"{titulo} {descripcion}"
            digitos = "".join(filter(str.isdigit, texto))
            
            if len(digitos) >= 10:
                telefono_paciente = "52" + digitos[-10:]
                nombre_paciente = extraer_nombre_limpio(titulo)

                start_dt = evento.get('start', {}).get('dateTime', '')
                hora_str = "10:00 am"
                if start_dt:
                    try:
                        dt_obj = datetime.fromisoformat(start_dt).astimezone(zona_mexico)
                        hora_str = dt_obj.strftime('%I:%M %p').lower()
                    except:
                        pass

                params = [
                    {"type": "text", "text": nombre_paciente},
                    {"type": "text", "text": doc_ocupacion},
                    {"type": "text", "text": "de hoy"},
                    {"type": "text", "text": hora_str},
                    {"type": "text", "text": doc_nombre}
                ]

                resp = enviar_mensaje(telefono_paciente, "template", template_params=params, template_name="confirmacion_cita")
                if resp and resp.status_code < 400:
                    total_enviados += 1
                    log(f"Recordatorio enviado a paciente {telefono_paciente}")

                    if supabase:
                        try:
                            supabase.table('metricas_y_registros').insert({
                                'calendar_id': cal_id,
                                'estado_accion': 'mensaje_enviado',
                                'confirmado': False
                            }).execute()
                        except Exception as ex:
                            log(f"Error guardando métrica de mensaje diario en Supabase: {ex}")

    return jsonify({"status": "ok", "enviados": total_enviados}), 200

@app.route('/ejecutar-encuesta-nocturna', methods=['POST'])
def ejecutar_encuesta_nocturna():
    if not supabase:
        return jsonify({"error": "Falta configuración de Supabase"}), 500

    try:
        response = supabase.table("Doctores").select("*").eq("enviar_encuesta", True).execute()
        doctores_activos = response.data if response.data else []

        if not doctores_activos:
            return jsonify({"status": "success", "message": "No hay doctores con encuesta activa hoy."}), 200

        zona_mexico = pytz.timezone('America/Mexico_City')
        ahora = datetime.now(zona_mexico)
        inicio = ahora.replace(hour=0, minute=0, second=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
        fin = ahora.replace(hour=23, minute=59, second=59).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')

        for doc in doctores_activos:
            cal_id = doc.get("calendar_id") or doc.get("email")
            doc_nombre = doc.get("name") or doc.get("nombre") or "Doctor"
            
            if not cal_id:
                continue

            calendario = obtener_servicio_calendar_por_doctor(cal_id)
            if not calendario:
                continue
            
            try:
                eventos = calendario.events().list(calendarId=cal_id, timeMin=inicio, timeMax=fin, singleEvents=True).execute().get('items', [])
            except Exception as e:
                log(f"Error leyendo calendario para encuesta {cal_id}: {e}")
                continue

            for evento in eventos:
                titulo = evento.get('summary', '')
                
                if "✅" not in titulo:
                    continue
                
                descripcion = evento.get('description', '')
                texto = f"{titulo} {descripcion}"
                digitos = "".join(filter(str.isdigit, texto))
                
                if len(digitos) >= 10:
                    telefono_paciente = "52" + digitos[-10:]
                    nombre_paciente = extraer_nombre_limpio(titulo)
                    
                    mensaje_encuesta = (
                        f"Hola *{nombre_paciente}*, de parte de *{doc_nombre}* esperamos que tu cita de hoy haya sido excelente. "
                        f"¿Qué tan satisfecho(a) te sientes con la atención recibida del 1 al 10? "
                        f"Puedes responder directamente a este mensaje con tu calificación y un breve comentario. ¡Gracias!"
                    )
                    
                    enviar_mensaje(telefono_paciente, "text", contenido=mensaje_encuesta)

        return jsonify({"status": "success", "message": "Encuestas nocturnas enviadas correctamente a citas confirmadas."}), 200

    except Exception as e:
        print(f"Error en encuesta nocturna: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

def marcar_evento_calendario(telefono_recibido, accion):
    tel_buscado = limpiar_telefono(telefono_recibido)
    if not supabase:
        return None, None
    
    try:
        response = supabase.table("Doctores").select("*").execute()
        doctores = response.data if response.data else []
    except:
        doctores = []

    zona_mexico = pytz.timezone('America/Mexico_City')
    ahora_mexico = datetime.now(zona_mexico)
    inicio = ahora_mexico.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
    fin = ahora_mexico.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
    
    simbolo = "✅" if accion == 'confirmar' else "❌"

    for doc in doctores:
        cal_id = doc.get("calendar_id") or doc.get("email")
        if not cal_id:
            continue
        
        calendario = obtener_servicio_calendar_por_doctor(cal_id)
        if not calendario:
            continue

        try:
            eventos_result = calendario.events().list(calendarId=cal_id, timeMin=inicio, timeMax=fin, singleEvents=True).execute()
            for evento in eventos_result.get('items', []):
                titulo = evento.get('summary', '')
                descripcion = evento.get('description', '')
                texto_completo = f"{titulo} {descripcion}"
                
                if tel_buscado in limpiar_telefono(texto_completo):
                    nombre_paciente = extraer_nombre_limpio(titulo)
                    titulo_limpio = titulo.replace(' ✅', '').replace(' ❌', '').replace('✅', '').replace('❌', '').strip()

                    if simbolo in titulo:
                        return doc, nombre_paciente
                    
                    nuevo_titulo = f"{titulo_limpio} {simbolo}"
                    
                    calendario.events().patch(
                        calendarId=cal_id, 
                        eventId=evento['id'], 
                        body={'summary': nuevo_titulo}
                    ).execute()
                    log(f"¡Calendario actualizado con {simbolo} para el evento: {nuevo_titulo}!")
                    return doc, nombre_paciente
        except Exception as e:
            log(f"Error actualizando calendario {cal_id}: {e}")
    return None, None

def buscar_doctor_y_paciente_por_telefono(telefono_recibido):
    tel_buscado = limpiar_telefono(telefono_recibido)
    if not supabase:
        return None, None
    
    try:
        response = supabase.table("Doctores").select("*").execute()
        doctores = response.data if response.data else []
    except:
        doctores = []

    zona_mexico = pytz.timezone('America/Mexico_City')
    ahora_mexico = datetime.now(zona_mexico)
    inicio = ahora_mexico.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')
    fin = ahora_mexico.replace(hour=23, minute=59, second=59, microsecond=0).astimezone(pytz.utc).isoformat().replace('+00:00', 'Z')

    for doc in doctores:
        cal_id = doc.get("calendar_id") or doc.get("email")
        if not cal_id:
            continue
        
        calendario = obtener_servicio_calendar_por_doctor(cal_id)
        if not calendario:
            continue

        try:
            eventos_result = calendario.events().list(calendarId=cal_id, timeMin=inicio, timeMax=fin, singleEvents=True).execute()
            for evento in eventos_result.get('items', []):
                titulo = evento.get('summary', '')
                descripcion = evento.get('description', '')
                texto_completo = f"{titulo} {descripcion}"
                
                if tel_buscado in limpiar_telefono(texto_completo):
                    nombre_paciente = extraer_nombre_limpio(titulo)
                    return doc, nombre_paciente
        except Exception as e:
            log(f"Error buscando doctor en calendario {cal_id}: {e}")
    return None, None

def procesar_webhook_asincrono(data):
    try:
        val = data['entry'][0]['changes'][0]['value']
        if 'messages' in val:
            msg = val['messages'][0]
            telefono_cliente = msg.get('from')
            
            texto = ""
            if msg.get('type') == 'button':
                texto = msg.get('button', {}).get('text', '').lower()
            elif msg.get('type') == 'text':
                texto = msg.get('text', {}).get('body', '').lower()
            
            log(f"Mensaje recibido de {telefono_cliente}: {texto}")

            doc_encontrado = buscar_doctor_por_telefono(telefono_cliente)
            if doc_encontrado:
                doc_nombre = doc_encontrado.get("name") or doc_encontrado.get("nombre") or "Doctor"
                doc_cal_id = doc_encontrado.get("calendar_id")

                if any(k in texto for k in ["empecemos"]):
                    resp_doc = '¡Perfecto! es un buen momento para empezar el día, "Stein tu Asistente Virtual" *activado*. \n *nota: Recuerda prepararte para epoca de lluvias*'
                    enviar_mensaje(telefono_cliente, "text", contenido=resp_doc)
                    return

                elif any(k in texto for k in ["trabajo el fin de semana", "trabajar fin de semana", "trabajo sabado y domingo"]):
                    zona_mexico = pytz.timezone('America/Mexico_City')
                    ahora = datetime.now(zona_mexico)
                    hoy_date = ahora.date()
                    
                    sabado_date = hoy_date + timedelta(days=((5 - hoy_date.weekday() + 7) % 7))
                    domingo_date = hoy_date + timedelta(days=((6 - hoy_date.weekday() + 7) % 7))
                    trabajar_str = f"{sabado_date},{domingo_date}"
                    
                    try:
                        supabase.table("Doctores").update({
                            "trabajar_fecha": trabajar_str
                        }).eq("calendar_id", doc_cal_id).execute()
                    except Exception as e:
                        log(f"Error guardando trabajar_fecha en Supabase: {e}")
                        
                    resp_doc = f"Entendido Dr. {doc_nombre}, he habilitado la agenda para trabajar este fin de semana ({sabado_date} y {domingo_date}). *Stein tu Asistente Virtual*"
                    enviar_mensaje(telefono_cliente, "text", contenido=resp_doc)
                    return

                elif any(k in texto for k in ["trabajo este sabado", "trabajo el sabado"]):
                    zona_mexico = pytz.timezone('America/Mexico_City')
                    ahora = datetime.now(zona_mexico)
                    hoy_date = ahora.date()
                    sabado_date = hoy_date + timedelta(days=((5 - hoy_date.weekday() + 7) % 7))
                    
                    try:
                        supabase.table("Doctores").update({
                            "trabajar_fecha": str(sabado_date)
                        }).eq("calendar_id", doc_cal_id).execute()
                    except Exception as e:
                        log(f"Error guardando trabajar_fecha en Supabase: {e}")
                        
                    resp_doc = f"Entendido Dr. {doc_nombre}, he habilitado la agenda para trabajar este sábado {sabado_date}. *Stein tu Asistente Virtual*"
                    enviar_mensaje(telefono_cliente, "text", contenido=resp_doc)
                    return

                elif any(k in texto for k in ["trabajo este domingo", "trabajo el domingo"]):
                    zona_mexico = pytz.timezone('America/Mexico_City')
                    ahora = datetime.now(zona_mexico)
                    hoy_date = ahora.date()
                    domingo_date = hoy_date + timedelta(days=((6 - hoy_date.weekday() + 7) % 7))
                    
                    try:
                        supabase.table("Doctores").update({
                            "trabajar_fecha": str(domingo_date)
                        }).eq("calendar_id", doc_cal_id).execute()
                    except Exception as e:
                        log(f"Error guardando trabajar_fecha en Supabase: {e}")
                        
                    resp_doc = f"Entendido Dr. {doc_nombre}, he habilitado la agenda para trabajar este domingo {domingo_date}. *Stein tu Asistente Virtual*"
                    enviar_mensaje(telefono_cliente, "text", contenido=resp_doc)
                    return

                elif any(k in texto for k in ["hoy no trabajo", "no trabajo", "descanso"]):
                    zona_mexico = pytz.timezone('America/Mexico_City')
                    ahora = datetime.now(zona_mexico)
                    fecha_hoy = ahora.date()
                    
                    fecha_pausa_fin = fecha_hoy
                    
                    match_fecha = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', texto)
                    if match_fecha:
                        dia, mes, anio = map(int, match_fecha.groups())
                        try:
                            fecha_pausa_fin = date(anio, mes, dia)
                        except ValueError:
                            pass
                    else:
                        dias_semana_map = {
                            "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, 
                            "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
                        }
                        for nombre_dia, dia_num in dias_semana_map.items():
                            if nombre_dia in texto:
                                dias_a_sumar = (dia_num - fecha_hoy.weekday() + 7) % 7
                                if dias_a_sumar == 0:
                                    dias_a_sumar = 7
                                fecha_pausa_fin = fecha_hoy + timedelta(days=dias_a_sumar)
                                break

                    fecha_pausa_str = str(fecha_pausa_fin)
                    
                    try:
                        supabase.table("Doctores").update({
                            "jornada_fecha": str(fecha_hoy),
                            "pausa_hasta": fecha_pausa_str
                        }).eq("calendar_id", doc_cal_id).execute()
                    except Exception as e:
                        log(f"Error actualizando pausa en Supabase: {e}")

                    if fecha_pausa_fin > fecha_hoy:
                        resp_doc = f"Entendido Dr. {doc_nombre}, he pausado las notificaciones desde hoy hasta el {fecha_pausa_str}. Disfrute sus vacaciones o descanso. *Stein tu Asistente Virtual*"
                    else:
                        resp_doc = f"Siempre es bueno tomarse el día para darse un respiro, bajar el cortisol y despejar la mente, que descanse *{doc_nombre}*. Hasta mañana *Stein tu Asistente Virtual*"
                    
                    enviar_mensaje(telefono_cliente, "text", contenido=resp_doc)
                    return
            
            if any(k in texto for k in ["sí, confirmar", "confirmar", "si"]):
                doc, nombre_paciente = marcar_evento_calendario(telefono_cliente, 'confirmar')
                if doc:
                    doc_nombre = doc.get("name") or doc.get("nombre") or "Doctor"
                    doc_cal_id = doc.get("calendar_id")
                    wa_link = doc.get("wa_link") or doc.get("link") or ""
                    
                    # NUEVO: Registro en la tabla de métricas y registros unificada
                    try:
                        supabase.table('metricas_y_registros').insert({
                            'calendar_id': doc_cal_id,
                            'estado_accion': 'cita_confirmada',
                            'confirmado': True
                        }).execute()
                    except Exception as e:
                        log(f"Error guardando métrica de confirmación en Supabase: {e}")

                    respuesta_texto = f"*¡Perfecto!* Se ha confirmado tu cita de hoy con {doc_nombre}. Dudas o aclaraciones, comunícate aquí: {wa_link}.\n*Nota: Recuerda prepararte para epoca de lluvias*\n *¡Que tenga un excelente día!*"
                    enviar_mensaje(telefono_cliente, "text", contenido=respuesta_texto)
                    
                    tel_doc = "".join(filter(str.isdigit, str(wa_link)))
                    if tel_doc:
                        enviar_mensaje(tel_doc, "text", contenido=f"✅ El paciente *{nombre_paciente}* ha confirmado su cita de hoy.")

            elif any(k in texto for k in ["no", "reagendar", "cancelar"]):
                doc, nombre_paciente = marcar_evento_calendario(telefono_cliente, 'reagendar')
                if doc:
                    doc_nombre = doc.get("name") or doc.get("nombre") or "Doctor"
                    doc_cal_id = doc.get("calendar_id")
                    wa_link = doc.get("wa_link") or doc.get("link") or ""

                    # NUEVO: Registro en la tabla de métricas y registros unificada para cancelación/reagendar
                    try:
                        supabase.table('metricas_y_registros').insert({
                            'calendar_id': doc_cal_id,
                            'estado_accion': 'cita_cancelada',
                            'confirmado': False
                        }).execute()
                    except Exception as e:
                        log(f"Error guardando métrica de cancelación en Supabase: {e}")

                    respuesta_texto = f"*Se ha cancelado tu cita.* Para reagendar, por favor comunícate con *{doc_nombre}*.\n *Da clic en el link de Whatsapp* aquí: {wa_link} con gusto atenderemos tu solicitud.\n *¡Que tenga un excelente día!*"
                    enviar_mensaje(telefono_cliente, "text", contenido=respuesta_texto)
                    
                    tel_doc = "".join(filter(str.isdigit, str(wa_link)))
                    if tel_doc:
                        enviar_mensaje(tel_doc, "text", contenido=f"❌ El paciente *{nombre_paciente}* indicó que necesita reagendar su cita de hoy.\n *IMPORTANTE* comunicate con él, para que no pierda su cita.")
            
            elif not doc_encontrado:
                match_cal = re.search(r'\b([1-9]|10)\b', texto)
                if match_cal:
                    calificacion = int(match_cal.group(1))
                    doc, nombre_paciente = buscar_doctor_y_paciente_por_telefono(telefono_cliente)
                    
                    if doc:
                        doc_cal_id = doc.get("calendar_id")
                        try:
                            zona_mexico = pytz.timezone('America/Mexico_City')
                            
                            supabase.table('metricas_y_registros').insert({
                                'calendar_id': doc_cal_id,
                                'estado_accion': 'encuesta_calificacion',
                                'calificacion': calificacion,
                                'confirmado': False
                            }).execute()
                            
                            log(f"Encuesta registrada: {calificacion} para calendar_id: {doc_cal_id}")
                        except Exception as e:
                            log(f"Error guardando encuesta en Supabase: {e}")

                    enviar_mensaje(telefono_cliente, "text", contenido="¡Muchas gracias por tu retroalimentación! La hemos registrado con éxito.")
                    return
    except Exception as e:
        log(f"Error general en procesar_webhook_asincrono: {e}")

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode and token and mode == 'subscribe' and token == VERIFY_TOKEN:
            return challenge, 200
        return 'Error de verificación', 403
    elif request.method == 'POST':
        data = request.json
        threading.Thread(target=procesar_webhook_asincrono, args=(data,)).start()
        return jsonify({"status": "received"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
