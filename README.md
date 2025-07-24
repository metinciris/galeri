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

# Günlük kaydı yapılandırma
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Ortam değişkenleri
LOCAL_REPO_BASE = "repos"  # Yerel repository'lerin saklanacağı klasör
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOCAL_REPO_BASE, exist_ok=True)
GITHUB_USER = "metinciris"  # Kullanıcı adınız
MAIN_GALERI_REPO = "galeri"  # Ana galeri repo adı

def get_available_repos():
    """Mevcut repoları listele."""
    repos = []
    for folder in os.listdir(LOCAL_REPO_BASE):
        if folder.startswith("gallery-"):
            repo_path = os.path.join(LOCAL_REPO_BASE, folder)
            if os.path.exists(os.path.join(repo_path, ".git")):
                repos.append(folder)
    return repos

def get_current_repo(available_repos):
    """Mevcut repository boyutunu kontrol et ve uygun olanı seç."""
    if not available_repos:
        return None
    max_size = 450 * 1024 * 1024  # 450 MB sınır (güvenli eşik)
    for repo_name in available_repos:
        repo_path = os.path.join(LOCAL_REPO_BASE, repo_name)
        total_size = sum(
            os.path.getsize(os.path.join(dirpath, f))
            for dirpath, _, filenames in os.walk(repo_path)
            for f in filenames
        )
        if total_size < max_size:
            return repo_name
    messagebox.showwarning("Uyarı", "Tüm repolar dolu. Yeni repo ekleyin ve listeyi yenileyin.")
    return None

def convert_svs_to_dzi(svs_path, outdir, uid, progress_callback):
    """SVS dosyasını vips dzsave ile DZI formatına dönüştür."""
    try:
        progress_callback(10, "SVS dosyası açılıyor")
        image = pyvips.Image.new_from_file(svs_path, access='sequential')
        
        progress_callback(20, "DZI dosyası ve karolar oluşturuluyor (10 bin karo bekleniyor)")
        image.dzsave(outdir, layout='dz', suffix='.jpeg[Q=80]', overlap=1, tile_size=256, depth='one')
        logger.info(f"DZI ve karolar oluşturuldu: {outdir}")

        progress_callback(90, "Görüntüleyici HTML oluşturuluyor")
        viewer_html = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Sanal Mikroskop</title>
    <script src="https://openseadragon.github.io/openseadragon/openseadragon.min.js"></script>
    <style>
        body { margin: 0; padding: 0; }
        #viewer { width: 100%; height: 100vh; }
    </style>
</head>
<body>
    <div id="viewer"></div>
    <script>
        OpenSeadragon({
            id: "viewer",
            prefixUrl: "https://openseadragon.github.io/openseadragon/images/",
            tileSources: "slide.dzi",
            showNavigator: true,
            animationTime: 0.5,
            maxZoomPixelRatio: 2
        });
    </script>
</body>
</html>
"""
        viewer_path = os.path.join(outdir, "index.html")
        with open(viewer_path, "w", encoding="utf-8") as f:
            f.write(viewer_html)
        logger.info(f"Görüntüleyici HTML oluşturuldu: {viewer_path}")

        progress_callback(100, "Dönüştürme tamamlandı")
        return True
    except Exception as e:
        logger.error(f"SVS dönüştürme hatası: {str(e)}")
        return False

def upload_to_gallery_repo(outdir, description, uid, blog_text, blog_image_path, repo_name, progress_callback):
    """Dosyaları belirtilen gallery repository'sine kaydet ve Git commit et."""
    try:
        progress_callback(0, f"Yerel Git repository'si hazırlanıyor: {repo_name}")
        repo_path = os.path.join(LOCAL_REPO_BASE, repo_name)
        slide_dir = os.path.join(repo_path, "slides", uid)
        
        # Repository var mı kontrol et, yoksa başlat
        if not os.path.exists(os.path.join(repo_path, ".git")):
            logger.info(f"Git repository'si bulunamadı, yeni başlatılıyor: {repo_path}")
            repo = git.Repo.init(repo_path)
            repo.create_remote("origin", f"https://github.com/{GITHUB_USER}/{repo_name}.git")
        else:
            repo = git.Repo(repo_path)
        
        # Slayt dosyalarını kopyala
        progress_callback(10, "Dosyalar repository'ye kopyalanıyor")
        os.makedirs(slide_dir, exist_ok=True)
        shutil.copytree(outdir, slide_dir, dirs_exist_ok=True)
        
        # Blog metni ve resim ekle
        progress_callback(30, "Blog metni ve resim ekleniyor")
        if blog_text:
            blog_text_path = os.path.join(slide_dir, "blog.txt")
            with open(blog_text_path, "w", encoding="utf-8") as f:
                f.write(blog_text)
            logger.info(f"Blog metni eklendi: {blog_text_path}")
        if blog_image_path:
            blog_image_dest = os.path.join(slide_dir, os.path.basename(blog_image_path))
            shutil.copy(blog_image_path, blog_image_dest)
            logger.info(f"Blog resmi eklendi: {blog_image_dest}")
        
        # README oluştur (page linki ile)
        progress_callback(40, "README oluşturuluyor")
        readme_content = f"# Slayt {uid}\n\nAçıklama: {description}\n\nPage Linki: https://{GITHUB_USER}.github.io/{repo_name}/slides/{uid}/\n\nBlog Metni:\n{blog_text}\n"
        readme_path = os.path.join(slide_dir, "README.md")
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(readme_content)
        logger.info(f"README eklendi: {readme_path}")

        # gallery.json güncelle
        progress_callback(50, "Galeri güncelleniyor")
        gallery_path = os.path.join(repo_path, "gallery.json")
        gallery = []
        if os.path.exists(gallery_path):
            with open(gallery_path, "r", encoding="utf-8") as f:
                gallery = json.load(f)
        gallery.append({
            "uid": uid,
            "title": description or f"Slayt {uid}",
            "description": description or "",
            "url": f"https://{GITHUB_USER}.github.io/{repo_name}/slides/{uid}/",
            "date": datetime.now().isoformat(),
            "blog": True if blog_text or blog_image_path else False
        })
        with open(gallery_path, "w", encoding="utf-8") as f:
            json.dump(gallery, f, indent=2, ensure_ascii=False)
        
        # index.html güncelle
        items = "\n".join(
            f"""<li><a href='{g['url']}' target='_blank'>{g['title']}</a> – {g['description']} (<a href='https://github.com/{GITHUB_USER}/{repo_name}/tree/main/slides/{g['uid']}' target='_blank'>Depo</a>)</li>"""
            for g in sorted(gallery, key=lambda x: x["date"], reverse=True)
        )
        html = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Sanal Mikroskop Galerisi</title>
</head>
<body>
    <h1>Sanal Mikroskop Slaytları</h1>
    <ul>""" + items + """</ul>
</body>
</html>"""
        index_path = os.path.join(repo_path, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)
        
        # Git işlemlerini yap
        progress_callback(90, "Dosyalar commit ediliyor")
        repo.index.add(["slides/" + uid, "gallery.json", "index.html"])
        repo.index.commit(f"Slayt {uid} eklendi")
        logger.info(f"Dosyalar commit edildi: {uid} ({repo_name})")

        progress_callback(100, f"Repository hazır: GitHub Desktop ile push edin (https://github.com/{GITHUB_USER}/{repo_name}.git)")
        return True
    except Exception as e:
        logger.error(f"Yerel Git hatası: {str(e)}")
        return False

def update_main_galeri_repo():
    """Ana galeri repository'sini güncelle (tüm gallery-<n> repolarını topla)."""
    try:
        main_repo_path = os.path.join(LOCAL_REPO_BASE, MAIN_GALERI_REPO)
        if not os.path.exists(main_repo_path):
            repo = git.Repo.init(main_repo_path)
            repo.create_remote("origin", f"https://github.com/{GITHUB_USER}/{MAIN_GALERI_REPO}.git")
            logger.info(f"Yerel ana galeri repository'si oluşturuldu: {main_repo_path}")
        
        repo = git.Repo(main_repo_path)
        
        # Tüm gallery-<n> repolarından veri topla
        gallery = []
        repo_count = 1
        while True:
            test_repo = f"gallery-{repo_count:02d}"
            test_path = os.path.join(LOCAL_REPO_BASE, test_repo, "gallery.json")
            if not os.path.exists(test_path):
                break
            with open(test_path, "r", encoding="utf-8") as f:
                repo_gallery = json.load(f)
                for item in repo_gallery:
                    item["repo"] = test_repo
                gallery.extend(repo_gallery)
            repo_count += 1
        
        # gallery.json oluştur
        gallery_path = os.path.join(main_repo_path, "gallery.json")
        with open(gallery_path, "w", encoding="utf-8") as f:
            json.dump(gallery, f, indent=2, ensure_ascii=False)
        
        # index.html oluştur (tüm slaytları listeleyen)
        items = "\n".join(
            f"""<li><a href='{g['url']}' target='_blank'>{g['title']}</a> – {g['description']} (<a href='https://github.com/{GITHUB_USER}/{g['repo']}/tree/main/slides/{g['uid']}' target='_blank'>Depo</a>)</li>"""
            for g in sorted(gallery, key=lambda x: x["date"], reverse=True)
        )
        html = """<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Ana Sanal Mikroskop Galerisi</title>
</head>
<body>
    <h1>Tüm Sanal Mikroskop Slaytları</h1>
    <ul>""" + items + """</ul>
</body>
</html>"""
        index_path = os.path.join(main_repo_path, "index.html")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)
        
        # Git commit
        repo.index.add(["gallery.json", "index.html"])
        repo.index.commit("Ana galeri güncellendi")
        logger.info("Ana galeri repository'si güncellendi")
        
        return True
    except Exception as e:
        logger.error(f"Ana galeri güncelleme hatası: {str(e)}")
        return False

def cleanup_files(svs_path, outdir):
    """Yerel dosyaları temizle (orijinal SVS silinmesin)."""
    try:
        shutil.rmtree(outdir, ignore_errors=True)
        logger.info(f"Yerel dosyalar temizlendi: {svs_path}, {outdir} (SVS silinmedi)")
    except Exception as e:
        logger.error(f"Temizlik hatası: {str(e)}")

def edit_html(repo_name):
    """Belirtilen repo'nun index.html dosyasını görsel editör ile düzenle."""
    repo_path = os.path.join(LOCAL_REPO_BASE, repo_name)
    index_path = os.path.join(repo_path, "index.html")
    if not os.path.exists(index_path):
        messagebox.showerror("Hata", f"index.html bulunamadı: {index_path}")
        return
    
    edit_window = tk.Toplevel()
    edit_window.title(f"HTML Düzenle: {repo_name}")
    edit_window.geometry("600x400")
    
    text_area = scrolledtext.ScrolledText(edit_window, width=70, height=20)
    text_area.pack(pady=10)
    
    with open(index_path, "r", encoding="utf-8") as f:
        text_area.insert(tk.INSERT, f.read())
    
    def save_html():
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(text_area.get("1.0", tk.END))
        messagebox.showinfo("Başarılı", "HTML kaydedildi.")
        edit_window.destroy()
    
    tk.Button(edit_window, text="Kaydet", command=save_html).pack(pady=10)

# GUI Arayüzü
def main_gui():
    root = tk.Tk()
    root.title("SVS Dönüştürme ve GitHub Entegrasyonu")
    root.geometry("600x700")

    # Scrollable frame ekle (en alt tuş gözüksün)
    canvas = tk.Canvas(root)
    scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
    scrollable_frame = ttk.Frame(canvas)

    scrollable_frame.bind(
        "<Configure>",
        lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")
        )
    )

    canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    # Slayt dosyası seç
    tk.Label(scrollable_frame, text="SVS Dosyası Seçin:").pack(pady=5)
    svs_path_var = tk.StringVar()
    tk.Entry(scrollable_frame, textvariable=svs_path_var, width=50).pack(pady=5)
    tk.Button(scrollable_frame, text="Dosya Seç", command=lambda: svs_path_var.set(filedialog.askopenfilename(filetypes=[("SVS Files", "*.svs")]))).pack(pady=5)

    # Açıklama
    tk.Label(scrollable_frame, text="Slayt Açıklama:").pack(pady=5)
    description_var = tk.StringVar()
    tk.Entry(scrollable_frame, textvariable=description_var, width=50).pack(pady=5)

    # Blog metni
    tk.Label(scrollable_frame, text="Blog Metni (İsteğe Bağlı):").pack(pady=5)
    blog_text_var = tk.Text(scrollable_frame, height=5, width=50)
    blog_text_var.pack(pady=5)

    # Blog resmi seç
    tk.Label(scrollable_frame, text="Blog Resmi Seçin (İsteğe Bağlı):").pack(pady=5)
    blog_image_var = tk.StringVar()
    tk.Entry(scrollable_frame, textvariable=blog_image_var, width=50).pack(pady=5)
    tk.Button(scrollable_frame, text="Resim Seç", command=lambda: blog_image_var.set(filedialog.askopenfilename(filetypes=[("Image Files", "*.jpg *.png")]))).pack(pady=5)

    # Repo seçimi
    tk.Label(scrollable_frame, text="Repo Seçin:").pack(pady=5)
    repo_var = tk.StringVar()
    repo_dropdown = ttk.Combobox(scrollable_frame, textvariable=repo_var, values=get_available_repos())
    repo_dropdown.pack(pady=5)
    tk.Button(scrollable_frame, text="Listeyi Yenile", command=lambda: repo_dropdown.config(values=get_available_repos())).pack(pady=5)

    # İlerleme barı
    progress = ttk.Progressbar(scrollable_frame, orient="horizontal", length=400, mode="determinate")
    progress.pack(pady=10)
    status_label = tk.Label(scrollable_frame, text="")
    status_label.pack(pady=5)

    def process_slide():
        svs_path = svs_path_var.get()
        description = description_var.get()
        blog_text = blog_text_var.get("1.0", tk.END).strip()
        blog_image_path = blog_image_var.get()
        repo_name = repo_var.get()
        
        if not svs_path:
            messagebox.showerror("Hata", "SVS dosyası seçilmedi.")
            return
        if not repo_name:
            messagebox.showerror("Hata", "Repo seçilmedi.")
            return
        
        uid = uuid.uuid4().hex[:8]
        outdir = os.path.join(UPLOAD_FOLDER, f"out-{uid}")
        os.makedirs(outdir, exist_ok=True)
        
        def progress_callback(value, msg):
            progress['value'] = value
            status_label.config(text=msg)
            root.update_idletasks()
        
        if not convert_svs_to_dzi(svs_path, outdir, uid, progress_callback):
            cleanup_files(svs_path, outdir)
            messagebox.showerror("Hata", "Dönüştürme başarısız. app.log kontrol edin.")
            return
        
        if not upload_to_gallery_repo(outdir, description, uid, blog_text, blog_image_path, repo_name, progress_callback):
            cleanup_files(svs_path, outdir)
            messagebox.showerror("Hata", "Gallery repo'ya ekleme başarısız. app.log kontrol edin.")
            return
        
        if not update_main_galeri_repo():
            messagebox.showwarning("Uyarı", "Ana galeri güncellenemedi, manuel kontrol edin.")
        
        messagebox.showinfo("Başarılı", f"Slayt {uid} hazırlandı. GitHub Desktop ile push edin.")
    
    tk.Button(scrollable_frame, text="Dönüştür ve Commit Et", command=process_slide).pack(pady=20)

    # HTML düzenleme butonu
    tk.Button(scrollable_frame, text="Ana Galeri HTML Düzenle", command=lambda: edit_html(MAIN_GALERI_REPO)).pack(pady=10)

    root.mainloop()

if __name__ == "__main__":
    main_gui()
```

### index.html - Ana galeri sayfası
```html
<!-- index.html - Ana galeri sayfası -->
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>Sanal Mikroskop Galerisi</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        ul { list-style-type: none; padding: 0; }
        li { margin: 10px 0; }
        .form-container { margin-top: 20px; }
    </style>
    <script>
        function startUpload(event) {
            const uid = 'uid-' + new Date().getTime();
            document.getElementById('uid').value = uid;
            setTimeout(() => {
                window.location.href = '/status/' + uid + '/view';
            }, 1000);
        }
    </script>
</head>
<body>
    <h1>Sanal Mikroskop Slaytları</h1>
    <ul>
        {% for item in gallery %}
        <li>
            <a href="{{ item.url }}" target="_blank">{{ item.title }}</a> – {{ item.description }}
            (<a href="https://github.com/{{ GITHUB_USER }}/{{ item.repo }}/tree/main/slides/{{ item.uid }}" target="_blank">Depo</a>)
            <form action="/rename/{{ item.uid }}" method="post" style="display:inline;">
                <input type="text" name="newname" placeholder="Yeni başlık">
                <button type="submit">Yeniden Adlandır</button>
            </form>
            <a href="/delete/{{ item.uid }}">Sil</a>
        </li>
        {% endfor %}
    </ul>
    <div class="form-container">
        <form action="/upload" method="post" enctype="multipart/form-data" onsubmit="startUpload(event)">
            <input type="file" name="file" accept=".svs" required>
            <input type="text" name="description" placeholder="Slayt açıklaması">
            <input type="hidden" id="uid" name="uid" value="">
            <button type="submit">Yükle</button>
        </form>
        <p>Yükleme tamamlandıktan sonra, GitHub Desktop ile uygun gallery repository'sini push edin. Durum sayfasında talimatları ve repository URL'sini bulabilirsiniz.</p>
    </div>
</body>
</html>
```

### status.html - İşlem durumu görselleştirme sayfası
```html
<!-- status.html - İşlem durumu görselleştirme sayfası -->
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <title>İşlem Durumu</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        #progress-bar { width: 100%; height: 20px; background: #f0f0f0; }
        #progress-fill { height: 100%; background: #4caf50; width: 0%; transition: width 0.5s; }
        .error { color: red; }
        .repo-url { margin-top: 10px; }
    </style>
    <script>
        function updateStatus(uid) {
            fetch('/status/' + uid)
                .then(response => response.json())
                .then(data => {
                    document.getElementById('status').innerText = `Durum: ${data.stage || 'Bilinmiyor'}`;
                    document.getElementById('progress-fill').style.width = `${data.progress || 0}%`;
                    document.getElementById('message').innerText = `Mesaj: ${data.message || ''}`;
                    document.getElementById('error').innerText = data.error ? `Hata: ${data.error}` : '';
                    if (data.stage === 'completed' && data.message.includes('GitHub Desktop')) {
                        document.getElementById('repo-url').innerHTML = `Repository: <a href="${data.message.split('(')[1].split(')')[0]}" target="_blank">${data.message.split('(')[1].split(')')[0]}</a>`;
                    }
                    if (data.stage !== 'completed' && !data.error) {
                        setTimeout(() => updateStatus(uid), 1000);
                    }
                })
                .catch(error => {
                    document.getElementById('error').innerText = `Hata: Durum alınamadı (${error})`;
                });
        }
    </script>
</head>
<body onload="updateStatus('{{ uid }}')">
    <h1>İşlem Durumu</h1>
    <p id="status">Durum: {{ initial_status.stage | default('Yükleniyor') }}</p>
    <div id="progress-bar">
        <div id="progress-fill" style="width: {{ initial_status.progress | default(0) }}%;"></div>
    </div>
    <p id="message">Mesaj: {{ initial_status.message | default('') }}</p>
    <p id="repo-url" class="repo-url"></p>
    <p id="error" class="error">{{ initial_status.error | default('') }}</p>
    <a href="/">Ana Sayfaya Dön</a>
</body>
</html>
```

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
