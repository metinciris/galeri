# main.py - Ana Python GUI uygulaması (SVS dönüştürme, Git commit ve galeri güncelleme)
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext
import os
import uuid
import shutil
import json
import logging
import pyvips
import git
from datetime import datetime

### .env - Ortam değişkenleri yapılandırma dosyası
```
# .env - Ortam değişkenleri yapılandırma dosyası
GITHUB_USERNAME=metinciris
LOCAL_REPO_BASE=repos
```

## Kullanım Talimatları (Türkçe)
1. **Uygulamayı Çalıştırın**:
   ```
   cd C:\slide-uploader
   python main.py
   ```
   - GUI açılacak.
2. **Slayt Yükleme**:
   - "Dosya Seç" ile SVS dosyası seçin.
   - Açıklama girin.
   - Blog metni yazın (isteğe bağlı).
   - "Resim Seç" ile blog resmi ekleyin (isteğe bağlı).
   - "Repo Seçin" dropdown'undan repo seçin (`gallery-01` vb.).
   - "Listeyi Yenile" ile mevcut repoları güncelleyin.
   - "Dönüştür ve Commit Et" butonuna tıklayın.
3. **İlerleme**:
   - İlerleme barı ve status mesajı, ayrıntılı aşamaları gösterir (ör. "DZI dosyası ve karolar oluşturuluyor (10 bin karo bekleniyor)", "Blog metni ve resim ekleniyor", "README oluşturuluyor").
4. **Hata Durumu**:
   - Hata olursa (ör. elektrik kesintisi), bozuk dosyalar otomatik silinir (`cleanup_files`).
   - Başarılı olanlar, GUI mesajıyla bildirilir.
5. **GitHub Desktop ile Push**:
   - Uygulama, slaytları `repos/gallery-01/slides/<uid>/` gibi dizine kaydeder ve commit eder.
   - Ana galeri (`repos/galeri`), JSON ve index.html ile güncellenir.
   - GitHub Desktop'ta ilgili repo'yu açın ve "Push origin" yapın.
6. **Blog Özelliği**:
   - Blog metni `slides/<uid>/blog.txt` olarak, resim ise orijinal adı ile kaydedilir.
   - README.md, page linki ile otomatik oluşturulur.
7. **HTML Düzenleme**:
   - "Ana Galeri HTML Düzenle" butonu ile `galeri/index.html` dosyasını açar ve kaydedilir.
8. **Güvenlik ve Yedekleme**:
   - Elektrik kesintisi için, try-except blokları dosyaları siler.
   - Başarılı işlemleri `app.log` veya GUI mesajından takip edin.

## Hata Giderme (Türkçe)
- **pyvips Hatası**: Libvips PATH'te değilse, `vips --version` çalışmaz. PATH'i kontrol edin.
- **Git Hatası**: GitHub Desktop ile repo klonlanmamışsa, manuel klonlayın.
- **Yavaşlık**: pyvips ile hızlanmalı, test edin. Eğer hala yavaşsa, SVS dosya boyutu büyük olabilir.
- **Log Kontrolü**: `C:\slide-uploader\app.log` dosyasını açın, hataları inceleyin.
- **Tuş Gözükmüyor**: Scrollable frame eklendi, pencereyi kaydırın.

### Klasör Yapısı
Uygulama, `C:\slide-uploader` klasöründe çalışır. Aşağıdaki yapıyı oluşturur:

- **uploads**: Geçici klasör, SVS dosyaları ve dönüştürme sonuçları (DZI, karolar, index.html) burada tutulur. İşlem bittikten sonra otomatik boşaltılır (hata olsa bile).
  - Örnek: `uploads\out-<uid>` (Dönüştürme klasörü, içinde `slide.dzi`, `slide_files` ve `index.html` bulunur).
- **repos**: GitHub repository'lerinin yerel kopyaları burada saklanır.
  - `repos\galeri`: Ana galeri repository'si. İçinde `gallery.json` (tüm slaytların meta verileri) ve `index.html` (tüm slaytların listesi) bulunur.
  - `repos\gallery-01` (ve gallery-02 vb.): Slayt repository'leri. İçinde:
    - `slides\<uid>`: Her slayt için alt klasör.
      - `slide.dzi` ve `slide_files`: Dönüştürülmüş karolar.
      - `index.html`: Slayt görüntüleyici.
      - `blog.txt`: Blog metni (isteğe bağlı).
      - `README.md`: Slayt açıklaması ve page linki.
      - Resim dosyası (ör. `serrated.JPG`, blog resmi).
    - `gallery.json`: Bu repo'daki slaytların meta verileri.
    - `index.html`: Bu repo'daki slaytların listesi.
- **app.log**: Uygulama log dosyası, hatalar ve işlem detayları burada tutulur.
- **main.py**: Uygulama kodu.
- **.env**: Yapılandırma dosyası (GITHUB_USERNAME vb.).

Klasörler otomatik oluşturulur, manuel müdahale gerekmez. `uploads` dışında her şey kalıcıdır.

### Nasıl Çalıştığı
Uygulama, Tkinter tabanlı bir GUI ile çalışır:
- SVS dosyası seçilir ve `uploads` klasörüne geçici olarak kopyalanır.
- pyvips ile SVS, DZI formatına dönüştürülür (hızlı karo oluşturma, 10 bin karo civarı).
- Dönüştürme sonuçları (`slide.dzi`, karolar, index.html) `uploads\out-<uid>` klasörüne kaydedilir.
- Sonuçlar, seçilen `gallery-<n>` repository'sine kopyalanır (`repos\gallery-01\slides\<uid>`).
- Blog metni/resim ve README.md eklenir.
- `gallery.json` ve `index.html` güncellenir.
- Ana `galeri` repository'si, tüm `gallery-<n>` repolarından veri toplar ve kendi `gallery.json` ile `index.html` dosyasını günceller.
- Git commit yapılır (push manuel, GitHub Desktop ile).
- `uploads` klasörü boşaltılır.

Tüm işlemler, try-except blokları ile korunur; hata olursa geçici dosyalar silinir.

### Nasıl İlerlediği
1. **Başlangıç**: GUI açılır, kullanıcı SVS dosyası, açıklama, blog metni/resim ve repo seçer.
2. **Dönüştürme**: SVS, vips dzsave ile DZI'ye dönüştürülür (20-100% ilerleme, karo oluşturma baskın).
3. **Kopyalama ve Ekleme**: Sonuçlar repo'ya taşınır, blog/README eklenir (30-50%).
4. **Güncelleme**: `gallery.json` ve `index.html` güncellenir (50-90%).
5. **Commit**: Git commit yapılır (90-100%).
6. **Ana Galeri Güncelleme**: Tüm repolardan veri toplanır, `galeri` güncellenir.
7. **Temizlik**: `uploads` boşaltılır.
8. **Sonuç**: GUI mesajı gösterir, push talimatı verir.

İlerleme barı ve status etiketi, her adımı gösterir (ör. "DZI dosyası ve karolar oluşturuluyor").

### Kullanıcının Kullanımı
1. `main.py` çalıştırın, GUI açılır.
2. SVS dosyası seçin (Dosya Seç butonu).
3. Açıklama girin.
4. Blog metni/resim ekleyin (isteğe bağlı).
5. Repo seçin (dropdown, yenile butonu ile güncellenir).
6. "Dönüştür ve Commit Et" tıklayın.
7. İlerlemeyi izleyin (bar ve mesaj).
8. Başarılı olursa mesaj alırsınız; GitHub Desktop ile push edin.
9. Hata olursa mesaj gösterilir, app.log kontrol edin.
10. "Ana Galeri HTML Düzenle" ile `galeri/index.html` düzenleyin.
11. Elektrik kesintisi olursa, bozuk dosyalar silinir; tekrar deneyin.

### Hataların Neler Olabileceği
1. **pyvips Hatası**: Libvips PATH'te yoksa veya SVS dosyası geçersizse. Çözüm: PATH kontrolü, vips --version test edin.
2. **Git Hatası**: Repo `.git` eksikse veya klonlanmamışsa. Çözüm: GitHub Desktop ile klonlayın veya `git init` yapın.
3. **Repo Doluluk**: 450 MB sınırı aşıldığında uyarı verir. Çözüm: Yeni repo ekleyin.
4. **Dosya Kopyalama Hatası**: Disk alanı yetersizse. Çözüm: Disk temizleyin.
5. **Commit Algılamama**: GitHub Desktop değişiklikleri görmüyorsa. Çözüm: Fetch origin yapın veya terminal ile `git status` kontrol edin.
6. **Yavaşlık**: Büyük SVS dosyası veya düşük CPU/RAM. Çözüm: Daha güçlü makine veya `depth='onetile'` deneyin.
7. **Tkinter Pencere Sorunu**: Pencere kapanmıyorsa. Çözüm: İşlem sonrası status_label güncellenir, manuel kapatın.
8. **Genel Hata**: `app.log` dosyasını inceleyin; tam hata mesajları orada.

Eğer bu çözüm çalışmazsa veya log paylaşırız, daha fazla yardımcı olabilirim.
