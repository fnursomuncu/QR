from fastapi import Cookie, FastAPI, HTTPException, Depends, Request, Form
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import psycopg2
import secrets
import re
from datetime import datetime, timedelta
from psycopg2.extras import RealDictCursor
from math import radians, cos, sin, asin, sqrt

# Eğer veritabanı ayarlarınız db.py içindeyse bu satır kalabilir.
from db import DB_CONFIG

templates = Jinja2Templates(directory="templates")
app = FastAPI(title="Yoklama Sistemi API")

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def read_root():
    """Ana dizine girildiğinde otomatik olarak öğrenci girişine yönlendirir."""
    return RedirectResponse(url="/instructor/login", status_code=302)

# ---------------------------------------------------------
# 1. VERİTABANI VE OTURUM BAĞIMLILIKLARI (DEPENDENCIES)
# ---------------------------------------------------------

def get_db():
    conn = psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()

def get_current_teacher(teacher_id: str = Cookie(None)):
    if not teacher_id:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim. Lütfen giriş yapın.")
    return int(teacher_id)

def get_current_student(student_id: str = Cookie(None)):
    if not student_id:
        raise HTTPException(status_code=401, detail="Yetkisiz erişim. Lütfen giriş yapın.")
    return int(student_id)

# ---------------------------------------------------------
# 2. YARDIMCI FONKSİYONLAR VE MODELLER
# ---------------------------------------------------------

class AttendanceRequest(BaseModel):
    student_id: int
    session_id: int
    device_uuid: str
    latitude: float
    longitude: float

class ManualUpdateReq(BaseModel):
    student_id: int
    session_id: int
    action: str  # 'add' veya 'remove'

def calculate_distance(lat1, lon1, lat2, lon2):
    if not all([lat1, lon1, lat2, lon2]):
        return 0
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1 
    dlat = lat2 - lat1 
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a)) 
    r = 6371000 # Dünya'nın yarıçapı
    return c * r

# ---------------------------------------------------------
# 3. GİRİŞ VE ÇIKIŞ İŞLEMLERİ (AUTH ROUTES)
# ---------------------------------------------------------

@app.get("/instructor/login")
def instructor_login_page(request: Request):
    """Öğretmen giriş sayfasını render eder."""
    return templates.TemplateResponse(request=request, name="instructor_login.html")

@app.get("/student/login")
@app.get("/student/qr-login")
def student_login_page(request: Request):
    """Öğrenci giriş sayfasını render eder."""
    return templates.TemplateResponse(request=request, name="student_login.html")

@app.post("/api/teacher/login")
def login_teacher(
    obs_id: str = Form(...), 
    sifre: str = Form(...), 
    db: psycopg2.extensions.connection = Depends(get_db)
):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT id, ad_soyad, password_hash FROM teacher WHERE obs_id = %s", (obs_id,))
        teacher = cursor.fetchone()
        
        if teacher and teacher['password_hash'] == sifre:
            response = RedirectResponse(url="/teacher/dashboard", status_code=302)
            response.set_cookie(key="teacher_id", value=str(teacher['id']), httponly=True)
            return response
        else:
            return RedirectResponse(url="/instructor/login?error=1", status_code=302)
    finally:
        cursor.close()

@app.post("/api/student/login")
def login_student(
    obs_id: str = Form(...),
    password_hash: str = Form(...),
    qr_token: str = Form(None),
    device_uuid: str = Form(None),
    latitude: str = Form(None), 
    longitude: str = Form(None),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    cursor = db.cursor()
    try:
        # 1. SADECE ÖĞRENCİ VAR MI VE ŞİFRE DOĞRU MU KONTROLÜ
        cursor.execute("SELECT id, ad_soyad, password_hash, device_uuid, device_change_count FROM student WHERE obs_id = %s", (obs_id,))
        student = cursor.fetchone()

        if not student or student['password_hash'] != password_hash:
            return RedirectResponse(url=f"/student/login?token={qr_token or ''}&error=1", status_code=302)

        student_id = student['id']
        target_url = "/student/panel" # Varsayılan hedef paneldir

        # 2. YOKLAMA İŞLEMİ (Cihaz kontrolü sadece burada yapılıyor!)
        if qr_token:
            # --- CİHAZ KONTROLÜ (Sadece Yoklama Alırken) ---
            if not device_uuid:
                return RedirectResponse(url=f"/student/login?token={qr_token}&error=no_device_id", status_code=302)

            reg_uuid = student['device_uuid']
            change_count = student['device_change_count'] or 0

            if reg_uuid is None:
                cursor.execute("UPDATE student SET device_uuid = %s WHERE id = %s", (device_uuid, student_id))
                db.commit()
            elif reg_uuid != device_uuid:
                if change_count < 1:
                    cursor.execute("UPDATE student SET device_uuid = %s, device_change_count = device_change_count + 1 WHERE id = %s", (device_uuid, student_id))
                    db.commit()
                else:
                    # Cihaz limiti dolduysa yoklama atamaz, geri gönder!
                    return RedirectResponse(url=f"/student/login?token={qr_token}&error=device_limit", status_code=302)
            # ---------------------------------------------

            # Cihaz kontrolü geçildi, yoklama kaydediliyor...
            cursor.execute("SELECT session_id FROM qr_code WHERE unique_token = %s", (qr_token,))
            qr_record = cursor.fetchone()

            if qr_record:
                session_id = qr_record['session_id']
                lat_val = float(latitude) if latitude and latitude.strip() else 0.0
                lon_val = float(longitude) if longitude and longitude.strip() else 0.0

                cursor.execute("SELECT latitude, longitude FROM session WHERE id = %s", (session_id,))
                sess_data = cursor.fetchone()
                class_lat, class_lon = sess_data['latitude'], sess_data['longitude']

                dist = calculate_distance(lat_val, lon_val, class_lat, class_lon)

                try:
                    cursor.execute(
                        "INSERT INTO attendance (session_id, student_id, proof_lat, proof_long, device_uuid_used, distance_meter) VALUES (%s, %s, %s, %s, %s, %s)",
                        (session_id, student_id, lat_val, lon_val, device_uuid, int(dist))
                    )
                    db.commit()
                    target_url = f"/student/attendance-success/{session_id}"
                except psycopg2.IntegrityError:
                    db.rollback()
                    target_url = "/student/panel?error=already_marked"
            else:
                target_url = "/student/login?error=invalid_token"

        # 3. OTURUMU AÇ VE YÖNLENDİR
        response = RedirectResponse(url=target_url, status_code=302)
        response.set_cookie(key="student_id", value=str(student_id), httponly=True)
        return response
    finally:
        cursor.close()

@app.get("/logout")
def logout_user():
    """Kullanıcının tarayıcısındaki çerezleri temizler."""
    response = RedirectResponse(url="/instructor/login", status_code=302)
    response.delete_cookie(key="teacher_id")
    response.delete_cookie(key="student_id")
    return response

# ---------------------------------------------------------
# 4. KONTROL PANELLERİ (DASHBOARDS)
# ---------------------------------------------------------

@app.get("/teacher/dashboard")
def teacher_dashboard(
    request: Request, 
    teacher_id: int = Depends(get_current_teacher),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT ad_soyad FROM teacher WHERE id = %s", (teacher_id,))
        teacher = cursor.fetchone()
        teacher_name = teacher['ad_soyad'] if teacher else "Misafir"
        
        cursor.execute("SELECT * FROM course WHERE teacher_id = %s", (teacher_id,))
        courses = cursor.fetchall()
    finally:
        cursor.close()
        
    return templates.TemplateResponse(
        request=request, 
        name="teacher_dashboard.html", 
        context={"teacher_name": teacher_name, "courses": courses}
    )

@app.get("/student/panel")
def student_dashboard(
    request: Request, 
    student_id: int = Depends(get_current_student),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT ad_soyad FROM student WHERE id = %s", (student_id,))
        student_data = cursor.fetchone()
        student_name = student_data['ad_soyad'] if student_data else "Bilinmeyen Öğrenci"

        cursor.execute("""
            SELECT c.course_code, c.course_name, 
                   TO_CHAR(s.start_time, 'DD.MM.YYYY') as start_time, 
                   TO_CHAR(a.timestamp, 'HH24:MI') as yoklama_saati
            FROM attendance a
            JOIN session s ON a.session_id = s.id
            JOIN course c ON s.course_id = c.id
            WHERE a.student_id = %s
            ORDER BY a.timestamp DESC
        """, (student_id,))
        yoklamalar = cursor.fetchall()
    finally:
        cursor.close()

    return templates.TemplateResponse(
        request=request, 
        name="student_panel.html", 
        context={"student_name": student_name, "yoklamalar": yoklamalar}
    )
# ---------------------------------------------------------
# 5. OTURUM (SESSION) VE YOKLAMA İŞLEMLERİ
# ---------------------------------------------------------

@app.post("/api/session/start")
def start_session(
    course_id: int = Form(...), 
    teacher_id: int = Depends(get_current_teacher),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT * FROM course WHERE id = %s AND teacher_id = %s", (course_id, teacher_id))
        course = cursor.fetchone()
        if not course:
            raise HTTPException(status_code=403, detail="Ders bulunamadı veya yetkiniz yok.")
            
        schedule_info = course['schedule_info'] or ""
        days_map = {
            'Monday': 'Pazartesi', 'Tuesday': 'Salı', 'Wednesday': 'Çarşamba',
            'Thursday': 'Perşembe', 'Friday': 'Cuma', 'Saturday': 'Cumartesi', 'Sunday': 'Pazar'
        }
        today_tr = days_map[datetime.now().strftime('%A')]
        
        start_time_str, end_time_str = None, None
        
        # YENİ VE ÇOK DAHA GÜVENLİ AYRIŞTIRMA MANTIĞI
        # Metnin içinde bugünün adını arıyoruz
        idx = schedule_info.find(today_tr)
        if idx != -1:
            # Günü bulduğu yerden sonrasını kes (Örn: "Pazar|12:00-15:00..." kısmını alır)
            sub_str = schedule_info[idx:]
            # Bu kısmın içindeki ilk iki saati Regex ile bul
            matches = re.findall(r'(\d{2}:\d{2})', sub_str)
            if len(matches) >= 2:
                start_time_str, end_time_str = matches[0], matches[1]
                
        if not start_time_str:
            return RedirectResponse(url="/teacher/dashboard?error=not_scheduled_today", status_code=302)
            
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M")
        
        current_slots = []
        temp_time = start_dt
        lesson_counter = 1
        
        while temp_time < end_dt:
            lesson_end = temp_time + timedelta(minutes=45)
            if lesson_end > end_dt + timedelta(minutes=1):
                break
            current_slots.append({'lesson_no': lesson_counter, 'start': temp_time, 'end': lesson_end})
            temp_time += timedelta(minutes=60)
            lesson_counter += 1
            
        active_slot = next((slot for slot in current_slots if (slot['start'] - timedelta(minutes=15)) <= now <= (slot['end'] + timedelta(minutes=15))), None)
        
        if not active_slot:
            return RedirectResponse(url="/teacher/dashboard?error=not_in_time_slot", status_code=302)
            
        sql_start_time = active_slot['start'].strftime("%Y-%m-%d %H:%M:%S")
        sql_end_time = active_slot['end'].strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute("SELECT id FROM session WHERE course_id = %s AND start_time = %s", (course_id, sql_start_time))
        existing_session = cursor.fetchone()
        
        if existing_session:
            session_id = existing_session['id']
        else:
            default_lat, default_lon = 39.9355, 32.8597
            cursor.execute("""
                INSERT INTO session (course_id, start_time, end_time, classroom_location, is_active, latitude, longitude) 
                VALUES (%s, %s, %s, 'Sınıf A', TRUE, %s, %s) RETURNING id
            """, (course_id, sql_start_time, sql_end_time, default_lat, default_lon))
            session_id = cursor.fetchone()['id']
            db.commit()

        return RedirectResponse(url=f"/session/{session_id}/qr", status_code=302)
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close() 

@app.get("/api/session/{session_id}/token")
def generate_qr_token(session_id: int, request: Request, db: psycopg2.extensions.connection = Depends(get_db)):
    token = secrets.token_hex(16)
    cursor = db.cursor()
    try:
        cursor.execute("INSERT INTO qr_code (session_id, unique_token, time_limit) VALUES (%s, %s, 25)", (session_id, token))
        db.commit()
        target_url = f"http://10.82.40.136:8000/student/qr-login?token={token}"
        return {"url": target_url}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

@app.get("/api/session/{session_id}/count")
def get_attendance_count(session_id: int, db: psycopg2.extensions.connection = Depends(get_db)):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT COUNT(*) as count FROM attendance WHERE session_id = %s", (session_id,))
        result = cursor.fetchone()
        return result['count'] if result else 0
    finally:
        cursor.close()

@app.post("/api/attendance/manual-update")
def manual_update_attendance(
    req: ManualUpdateReq, 
    teacher_id: int = Depends(get_current_teacher), 
    db: psycopg2.extensions.connection = Depends(get_db)
):
    cursor = db.cursor()
    try:
        if req.action == 'add':
            cursor.execute("SELECT id FROM attendance WHERE session_id = %s AND student_id = %s", (req.session_id, req.student_id))
            if not cursor.fetchone():
                cursor.execute("""
                    INSERT INTO attendance (session_id, student_id, device_uuid_used, distance_meter, timestamp) 
                    VALUES (%s, %s, 'MANUEL', 0, CURRENT_TIMESTAMP)
                """, (req.session_id, req.student_id))
            db.commit()
            return {"status": "success", "new_time": datetime.now().strftime("%H:%M")}
        elif req.action == 'remove':
            cursor.execute("DELETE FROM attendance WHERE session_id = %s AND student_id = %s", (req.session_id, req.student_id))
            db.commit()
            return {"status": "success"}
        else:
            raise HTTPException(status_code=400, detail="Geçersiz işlem")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()

# ---------------------------------------------------------
# 6. EKRAN RENDER ROUTE'LARI (QR, LİSTE, RAPOR)
# ---------------------------------------------------------

@app.get("/session/{session_id}/qr")
def qr_screen(request: Request, session_id: int, teacher_id: int = Depends(get_current_teacher), db: psycopg2.extensions.connection = Depends(get_db)):
    cursor = db.cursor()
    try:
        cursor.execute("""
            SELECT s.*, c.course_name, c.course_code 
            FROM session s JOIN course c ON s.course_id = c.id WHERE s.id = %s
        """, (session_id,))
        current_session = cursor.fetchone()
        if not current_session:
            raise HTTPException(status_code=404, detail="Oturum bulunamadı")

        cursor.execute("""
            SELECT id, start_time FROM session 
            WHERE course_id = %s AND DATE(start_time) = DATE(%s) ORDER BY start_time ASC
        """, (current_session['course_id'], current_session['start_time']))
        all_sessions = cursor.fetchall()
    finally:
        cursor.close()
        
    return templates.TemplateResponse(
        request=request, 
        name="qr_generate.html", 
        context={"current_session": current_session, "all_sessions": all_sessions}
    )

@app.get("/session/{session_id}/attendance")
def attendance_list_screen(request: Request, session_id: int, source: str = "", teacher_id: int = Depends(get_current_teacher), db: psycopg2.extensions.connection = Depends(get_db)):
    cursor = db.cursor()
    try:
        cursor.execute("""
            SELECT s.start_time, c.id as course_id, c.course_name, c.course_code 
            FROM session s JOIN course c ON s.course_id = c.id WHERE s.id = %s
        """, (session_id,))
        session_info = cursor.fetchone()

        cursor.execute("""
            SELECT s.id AS student_db_id, s.obs_id, s.ad_soyad, a.timestamp AS giris_zamani
            FROM session ses
            JOIN enrollment e ON ses.course_id = e.course_id
            JOIN student s ON e.student_id = s.id
            LEFT JOIN attendance a ON s.id = a.student_id AND a.session_id = ses.id
            WHERE ses.id = %s ORDER BY s.obs_id ASC
        """, (session_id,))
        students = cursor.fetchall()
    finally:
        cursor.close()

    time_diff = (datetime.now() - session_info['start_time']).total_seconds()
    is_past_session = time_diff > (50 * 60)
    show_qr_button = not is_past_session and source != 'report'

    return templates.TemplateResponse(
        request=request, 
        name="yoklama.html", 
        context={
            "session_info": session_info, 
            "students": students,
            "session_id": session_id, 
            "source": source, 
            "is_past_session": is_past_session, 
            "show_qr_button": show_qr_button
        }
    )

@app.get("/course/{course_id}/students")
def course_students_page(request: Request, course_id: int, teacher_id: int = Depends(get_current_teacher), db: psycopg2.extensions.connection = Depends(get_db)):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT * FROM course WHERE id = %s", (course_id,))
        course = cursor.fetchone()
        cursor.execute("""
            SELECT s.id, s.obs_id, s.ad_soyad, s.email FROM student s
            JOIN enrollment e ON s.id = e.student_id WHERE e.course_id = %s ORDER BY s.ad_soyad ASC
        """, (course_id,))
        students = cursor.fetchall()
    finally:
        cursor.close()

    return templates.TemplateResponse(
        request=request, 
        name="course_students.html", 
        context={
            "course": course, 
            "students": students,
            "message": request.query_params.get("message"), 
            "msg_type": request.query_params.get("msg_type")
        }
    )

@app.get("/course/{course_id}/report")
def course_report_page(request: Request, course_id: int, teacher_id: int = Depends(get_current_teacher), db: psycopg2.extensions.connection = Depends(get_db)):
    cursor = db.cursor()
    try:
        cursor.execute("SELECT * FROM course WHERE id = %s", (course_id,))
        course = cursor.fetchone()
        
        cursor.execute("SELECT * FROM session WHERE course_id = %s ORDER BY start_time ASC", (course_id,))
        sessions = cursor.fetchall()
        
        cursor.execute("""
            SELECT s.id, s.obs_id, s.ad_soyad FROM student s
            JOIN enrollment e ON s.id = e.student_id WHERE e.course_id = %s ORDER BY s.obs_id ASC
        """, (course_id,))
        students = cursor.fetchall()
        
        attendance_map = {}
        if sessions:
            session_ids = tuple(s['id'] for s in sessions)
            if len(session_ids) == 1:
                cursor.execute("SELECT session_id, student_id FROM attendance WHERE session_id = %s", (session_ids[0],))
            else:
                cursor.execute("SELECT session_id, student_id FROM attendance WHERE session_id IN %s", (session_ids,))
            
            for row in cursor.fetchall():
                s_id, st_id = row['session_id'], row['student_id']
                if s_id not in attendance_map:
                    attendance_map[s_id] = {}
                attendance_map[s_id][st_id] = True

        student_stats = {}
        total_sessions = len(sessions)
        
        for student in students:
            st_id = student['id']
            present_count = 0
            for sess in sessions:
                s_id = sess['id']
                if s_id in attendance_map and st_id in attendance_map[s_id]:
                    present_count += 1
            
            ratio = round((present_count / total_sessions) * 100) if total_sessions > 0 else 0
            color = "success" if ratio >= 70 else ("warning" if ratio >= 50 else "danger")
            
            student_stats[st_id] = {"present_count": present_count, "total_count": total_sessions, "ratio": ratio, "color": color}
    finally:
        cursor.close()

    return templates.TemplateResponse(
        request=request, 
        name="course_report.html", 
        context={
            "course": course, 
            "sessions": sessions,
            "students": students, 
            "attendance_map": attendance_map, 
            "student_stats": student_stats
        }
    )

# ---------------------------------------------------------
# 7. YÖNETİCİ (ADMIN) PANELİ İŞLEMLERİ
# ---------------------------------------------------------

# Admin yetki kontrolü
def get_current_admin(admin_logged_in: str = Cookie(None)):
    if not admin_logged_in or admin_logged_in != "true":
        raise HTTPException(status_code=401, detail="Yetkisiz erişim. Lütfen admin girişi yapın.")
    return True

@app.get("/admin/login")
def admin_login_page(request: Request):
    """Admin giriş ekranı"""
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"error": request.query_params.get("error")})

@app.post("/api/admin/login")
def admin_login_post(password: str = Form(...)):
    """Admin şifre kontrolü"""
    # PHP kodunuzdaki sabit şifreyi kullanıyoruz
    if password == "a1d2m3in": 
        response = RedirectResponse(url="/admin/dashboard", status_code=302)
        response.set_cookie(key="admin_logged_in", value="true", httponly=True)
        return response
    else:
        return RedirectResponse(url="/admin/login?error=Hatalı şifre", status_code=302)

@app.get("/admin/logout")
def admin_logout():
    """Admin çıkış"""
    response = RedirectResponse(url="/admin/login", status_code=302)
    response.delete_cookie("admin_logged_in")
    return response

@app.get("/admin/dashboard")
def admin_dashboard(request: Request, is_admin: bool = Depends(get_current_admin), db: psycopg2.extensions.connection = Depends(get_db)):
    """Admin paneli arayüzü ve veritabanı tablolarının çekilmesi"""
    cursor = db.cursor()
    try:
        cursor.execute("SELECT * FROM teacher ORDER BY id DESC")
        teachers = cursor.fetchall()
        
        cursor.execute("SELECT * FROM student ORDER BY id DESC")
        students = cursor.fetchall()
        
        cursor.execute("""
            SELECT course.*, teacher.ad_soyad as hoca_adi 
            FROM course 
            LEFT JOIN teacher ON course.teacher_id = teacher.id 
            ORDER BY course.id DESC
        """)
        courses = cursor.fetchall()
        
        cursor.execute("""
            SELECT e.student_id, e.course_id, s.ad_soyad, s.obs_id, c.course_code, c.course_name
            FROM enrollment e
            JOIN student s ON e.student_id = s.id
            JOIN course c ON e.course_id = c.id
            ORDER BY c.course_code ASC
        """)
        enrollments = cursor.fetchall()
        
    finally:
        cursor.close()
    
    return templates.TemplateResponse(
        request=request, 
        name="admin_dashboard.html", 
        context={
            "teachers": teachers, 
            "students": students, 
            "courses": courses, 
            "enrollments": enrollments,
            "message": request.query_params.get("message"),
            "msg_type": request.query_params.get("msg_type")
        }
    )

@app.post("/api/admin/add")
def admin_add_item(
    request: Request,
    form_action: str = Form(...),
    obs_id: str = Form(None),
    ad_soyad: str = Form(None),
    email: str = Form(None),
    password: str = Form(None),
    teacher_id: int = Form(None),
    faculty: str = Form(None),
    department: str = Form(None),
    course_code: str = Form(None),
    course_name: str = Form(None),
    group_name: str = Form(None),
    schedule_info: str = Form(None),
    kontenjan: int = Form(None),
    credit: int = Form(None),
    student_id: int = Form(None),
    course_id: int = Form(None),
    is_admin: bool = Depends(get_current_admin),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    """Admin panelinden gelen EKLEME işlemlerini yönetir"""
    cursor = db.cursor()
    msg, msg_type = "", "success"
    try:
        if form_action == "add_teacher":
            cursor.execute("INSERT INTO teacher (obs_id, ad_soyad, email, password_hash) VALUES (%s, %s, %s, %s)", (obs_id, ad_soyad, email, password))
            msg = "Hoca Eklendi."
        elif form_action == "add_student":
            cursor.execute("INSERT INTO student (obs_id, ad_soyad, email, password_hash) VALUES (%s, %s, %s, %s)", (obs_id, ad_soyad, email, password))
            msg = "Öğrenci Eklendi."
        elif form_action == "add_course":
            cursor.execute("""
                INSERT INTO course (teacher_id, faculty, department, course_code, course_name, group_name, schedule_info, kontenjan, credit) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (teacher_id, faculty, department, course_code, course_name, group_name, schedule_info, kontenjan, credit))
            msg = "Ders Eklendi."
        elif form_action == "enroll_student":
            cursor.execute("SELECT * FROM enrollment WHERE student_id = %s AND course_id = %s", (student_id, course_id))
            if cursor.fetchone():
                msg, msg_type = "Öğrenci zaten bu derse kayıtlı.", "info"
            else:
                cursor.execute("INSERT INTO enrollment (student_id, course_id) VALUES (%s, %s)", (student_id, course_id))
                msg = "Öğrenci derse kaydedildi."
        db.commit()
    except Exception as e:
        db.rollback()
        msg, msg_type = f"Hata: {str(e)}", "error"
    finally:
        cursor.close()
    return RedirectResponse(url=f"/admin/dashboard?message={msg}&msg_type={msg_type}", status_code=302)

@app.get("/api/admin/action")
def admin_actions(
    type: str, action: str, 
    id: int = None, sid: int = None, cid: int = None,
    is_admin: bool = Depends(get_current_admin),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    """Admin panelinden gelen SİLME ve CİHAZ SIFIRLAMA işlemlerini yönetir"""
    cursor = db.cursor()
    msg, msg_type = "", "success"
    try:
        if action == "delete":
            if type == "teacher" and id:
                cursor.execute("DELETE FROM teacher WHERE id = %s", (id,))
                msg = "Hoca silindi."
            elif type == "student" and id:
                cursor.execute("DELETE FROM student WHERE id = %s", (id,))
                msg = "Öğrenci silindi."
            elif type == "course" and id:
                cursor.execute("DELETE FROM course WHERE id = %s", (id,))
                msg = "Ders silindi."
            elif type == "enrollment" and sid and cid:
                cursor.execute("DELETE FROM enrollment WHERE student_id = %s AND course_id = %s", (sid, cid))
                msg = "Ders kaydı silindi."
        elif action == "reset_device" and type == "student" and id:
            # Cihazı sıfırla VE değiştirme hakkını geri ver!
            cursor.execute("UPDATE student SET device_uuid = NULL, device_change_count = 0 WHERE id = %s", (id,))
            msg, msg_type = "Cihaz kilidi sıfırlandı.", "info"
        db.commit()
    except Exception as e:
        db.rollback()
        msg, msg_type = f"Hata: {str(e)}", "error"
    finally:
        cursor.close()
    return RedirectResponse(url=f"/admin/dashboard?message={msg}&msg_type={msg_type}", status_code=302)

@app.get("/student/attendance-success/{session_id}")
def attendance_success_page(
    request: Request,
    session_id: int,
    student_id: int = Depends(get_current_student),
    db: psycopg2.extensions.connection = Depends(get_db)
):
    """Öğrenciye yoklama işleminin sonucunu gösteren onay ekranı"""
    cursor = db.cursor()
    try:
        cursor.execute("""
            SELECT c.course_code, c.course_name, t.ad_soyad, a.timestamp
            FROM attendance a
            JOIN session s ON a.session_id = s.id
            JOIN course c ON s.course_id = c.id
            JOIN teacher t ON c.teacher_id = t.id
            WHERE a.student_id = %s AND a.session_id = %s
        """, (student_id, session_id))
        record = cursor.fetchone()

        if not record:
            return RedirectResponse(url="/student/panel", status_code=302)

        dersBilgisi = f"{record['course_code']} - {record['course_name']}"
        ogretmen = record['ad_soyad']
        tarihSaat = record['timestamp'].strftime("%d.%m.%Y - %H:%M")

        cursor.execute("SELECT ad_soyad FROM student WHERE id = %s", (student_id,))
        student_data = cursor.fetchone()
        student_name = student_data['ad_soyad'] if student_data else "Öğrenci"

    finally:
        cursor.close()

    return templates.TemplateResponse(
        request=request,
        name="yoklama_onay.html",
        context={
            "student_name": student_name,
            "dersBilgisi": dersBilgisi,
            "ogretmen": ogretmen,
            "tarihSaat": tarihSaat,
            "message": "Yoklama Başarıyla Kaydedildi!",
            "status_icon": "✔",
            "is_success": True
        }
    )