# İNADINA TV Müzik Çalar

Yerel müzik koleksiyonunuzu tarayan, Türkçe arayüze sahip web tabanlı müzik çalar.

## Özellikler

- MP3, FLAC, M4A, WAV, OGG, OPUS, WMA ve AAC dosyalarını tarama
- Sanatçı, albüm, çalma listesi ve arama görünümleri
- Çalma sırası, karışık çalma, tekrar ve indirme
- Gömülü kapak ve şarkı sözü görüntüleme
- Etiketleri (başlık, sanatçı, albüm, tür, yıl ve sözler) düzenleme
- Deezer üzerinden Türkçe müzik metadata ve kapak arama
- Telefon ve masaüstü ekranlarına uyumlu karanlık tasarım

> Telifli müzik dosyaları projeye dahil edilmez. Uygulama, sizin cihazınızdaki/ sunucudaki müzik klasörlerini tarar. Deezer entegrasyonu yalnızca arama ve metadata içindir.

## Kurulum

Python 3.10+ ve etiket yazımı için `ffmpeg` gereklidir:

```bash
# Debian/Ubuntu
sudo apt-get install ffmpeg

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Çalıştırma

```bash
python app.py
```

Tarayıcıdan [http://localhost:5000](http://localhost:5000) adresini açın ve **＋ Klasör** düğmesiyle müzik klasörünüzü ekleyin. Seçilen klasör yolu `config.json` içinde saklanır; müzik dosyaları kopyalanmaz.

## Proje yapısı

- `app.py`: Flask sunucusu ve müzik kütüphanesi API'si
- `templates/index.html`: Türkçe web arayüzü
- `requirements.txt`: Python bağımlılıkları

## Lisans

Kişisel kullanım için hazırlanmıştır. Müzik dosyalarının kullanım hakları kullanıcıya aittir.
