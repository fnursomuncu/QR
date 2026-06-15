# 📱 Akıllı QR Kod Tabanlı Yoklama Sistemi

Bu proje; öğrencilerin uzaktan, başkalarının yerine veya sahte konumlarla yoklama vermesini engellemek amacıyla geliştirilmiş, yüksek güvenlikli ve çok katmanlı bir mobil/web yoklama yönetim sistemidir.

---

## 🚀 Uygulamayı Çalıştırmak

Uygulamayı yerel sunucunuzda ayağa kaldırmak için terminalde aşağıdaki komutu çalıştırmanız yeterlidir:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000


🔒 1. Güvenlik ve Doğrulama Kontrolleri (Sistemin Kalbi)
Uygulamanın en güçlü yanı, suistimalleri sıfıra indiren çok katmanlı güvenlik mimarisidir:

🔒 Cihaz Kimliği (Fingerprint UUID) Kontrolü
Sistem, her öğrenci hesabını giriş yapılan ilk cihaza zimmetler. Farklı bir telefondan veya hesaptan giriş yapılmaya çalışıldığında sistem anında device_limit hatası verir ve erişimi engeller.

📍 GPS ve Konum Doğrulaması
Yoklama esnasında cihazın anlık koordinatları (enlem/boylam) alınır. Haversine formülü kullanılarak sınıfın merkez koordinatları ile arasındaki mesafe metre cinsinden hesaplanır. Sınıf sınırları içinde olmayan kullanıcılar yoklama veremez.

⏳ Dinamik QR Kod (Token)
Öğretmen yoklamayı başlattığında, arka planda 16 karakterli, benzersiz (unique) ve belirli bir süre geçerli olan bir token üretilir. Bu token olmadan doğrudan yoklama URL'sine gitmek geçersizdir.

📅 Zaman ve Ders Çizelgesi Doğrulaması
Ders programı yoğunluğunun yüksek olduğu günlerde bile sistem, Regex (Regular Expressions) kullanarak o anki tarih ve saatte hangi dersin aktif olduğunu arka planda tespit eder. Sadece doğru zaman dilimindeki derse oturum açılmasına izin verir.

👥 2. Kullanıcı Rolleri ve Yetenekleri
Sistem üç farklı kullanıcı rolü (Aktris/Aktör) üzerine inşa edilmiştir:

👨‍🎓 Öğrenci (Student) Modülü
Giriş: Öğrenci numarası ve şifresiyle güvenli oturum açma.

Yoklama: Öğretmenin yansıttığı QR kodu okutarak anlık konum ve cihaz doğrulama testlerinden geçiş.

Geri Bildirim: İşlem başarılı olduğunda anlık "Yoklama Başarıyla Kaydedildi" onayı.

Geçmiş: Kendi panelinden hangi derse, hangi gün ve saatte katıldığının tam listesini görüntüleme.

👩‍🏫 Öğretmen (Teacher) Modülü
Ders Yönetimi: Kendisine atanan derslerin listesini ve detaylarını görme.

Güvenli Oturum: Sistemi manipüle etmek isteyenlere karşı dinamik ve güvenli QR ekranını sınıfa yansıtma.

Canlı Takip: Yoklama ekranında derse katılan öğrencileri saniye saniye (real-time) izleme.

Manuel Müdahale: Şarjı biten veya teknik sorun yaşayan öğrenciler için sistem üzerinden manuel öğrenci ekleme/çıkarma yetkisi.

Raporlama: Dönem sonu başarı ve devamsızlık yüzdelerini renk kodlarıyla (Yeşil, Sarı, Kırmızı) gösteren gelişmiş analitik raporlama.

⚡ Yönetici (Admin) Modülü
⚠️ Erişim Notu: Sistemin en yetkili modudur. Varsayılan erişim şifresi a1d2m3in olarak belirlenmiştir.

Veri Yönetimi: Sisteme yeni öğretmenler, öğrenciler ve dersler ekleme; öğrencileri derslere atama (Enrollment).

Cihaz Sıfırlama: Telefon değiştiren veya hesabı kilitlenen öğrencilerin Cihaz Kilidini (Reset Device) sıfırlayarak yeniden giriş hakkı tanıma.
