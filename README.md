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

Eğer ek özellik (ör. otomatik push, blog linki otomatik ekleme) isterseniz, belirtin!****
