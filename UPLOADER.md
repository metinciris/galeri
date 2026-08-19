# Kendi Whole-Slide Galerini Kurma ve Geliştirme

Bu repository yalnızca mevcut sanal mikroskop galerisini yayınlamak için değil, aynı yapıyı kendi GitHub hesabında kurmak isteyenler ve uploader yazılımını geliştirmek isteyenler için de örnek bir projedir.

Ana araç: [`whole_slide_uploader.py`](whole_slide_uploader.py)

> **Önemli:** `.env` içindeki GitHub token'ını hiçbir zaman GitHub'a yüklemeyin. Repository'deki `.gitignore` bunu engellemek için `.env` dosyasını dışlar.

## Ne yapar?

`whole_slide_uploader.py`, bir veya birden fazla `.svs` whole-slide dosyasını masaüstü arayüzü üzerinden hazırlar ve GitHub Pages üzerinde ayrı sanal mikroskop sayfaları olarak yayımlar.

Temel akış:

1. `yüklenecek/` klasöründeki tüm `.svs` dosyaları bulunur.
2. Her slayt için arayüzde başlık, açıklama ve isteğe bağlı thumbnail hazırlanır.
3. Thumbnail verilmezse SVS dosyasından otomatik üretilir; büyük görseller küçültülür.
4. Slayt DeepZoom (`slide.dzi` + `slide_files/`) biçimine çevrilir.
5. Her slayt için `gallery-XXX` repository oluşturulur veya yarım kalan mevcut repository ile devam edilir.
6. GitHub Pages etkinleştirilir.
7. Yayındaki sayfa ve `slide.dzi` gerçekten erişilebilir olana kadar doğrulama yapılır.
8. Ana `galeri` repository'si güncellenir ve yeni slayt ana sayfada görünür hale gelir.
9. İşlem başarıyla tamamlanınca kaynak SVS `yüklenen/` klasörüne taşınır.
10. Yerel `repos/gallery-XXX` kopyası ancak remote commit ve web yayını doğrulandıktan sonra güvenli biçimde silinebilir.

Elektrik kesintisi, internet kopması veya yarım GitHub yüklemesi durumunda işlem bilgileri diskte tutulur. Program yeniden açıldığında mümkün olduğunca aynı repository üzerinden devam eder ve gereksiz kopya repository oluşturmaz.

## Önerilen klasör yapısı

```text
whole-slide-uploader/
├─ whole_slide_uploader.py
├─ .env
├─ yüklenecek/
├─ yüklenen/
└─ repos/
```

Bu klasörlerin çoğu ilk çalıştırmada otomatik oluşturulur.

## Gereksinimler

- Python 3
- Git
- libvips
- Python paketleri: `requests`, `pyvips`
- Tkinter destekli Python kurulumu
- GitHub hesabı ve uygun yetkilere sahip bir personal access token

Python paketleri:

```bash
pip install -r requirements.txt
```

`pyvips` Python paketi tek başına yeterli değildir; işletim sisteminde **libvips** de kurulu olmalıdır.

## `.env` ayarı

Önce örneği kopyalayın:

```text
.env.example -> .env
```

En az şu alanları doldurun:

```env
GITHUB_USERNAME=your-github-username
GITHUB_TOKEN=your-secret-token
LOCAL_REPO_BASE=repos
```

Ana galeri repository adınız `galeri` değilse:

```env
GALLERY_REPO_NAME=my-gallery
```

Slayt repository adları varsayılan olarak şöyle oluşturulur:

```text
gallery-001
gallery-002
gallery-003
...
```

Bunlar `REPO_PREFIX` ve `REPO_DIGITS` ile değiştirilebilir.

## Çalıştırma

Windows örneği:

```bat
cd C:\whole-slide-uploader
python whole_slide_uploader.py
```

Varsayılan kullanım masaüstü arayüzünü açar.

## Arayüz

Arayüzün amacı çok sayıda slaytta bile işlemi anlaşılır tutmaktır.

### Slayt hazırlığı

Her SVS için:

- **Başlık**
- **Açıklama**
- **Thumbnail**

alanları bulunur.

Thumbnail seçmek zorunlu değildir. Seçilmezse slayttan otomatik oluşturulur. Varsayılan sınırlar:

- maksimum yaklaşık `1000 px`
- hedef yaklaşık `500 KB` JPEG

Bunlar `.env` içindeki `THUMB_MAX_PX` ve `THUMB_TARGET_KB` ile değiştirilebilir.

### Çoklu yükleme

Birden fazla SVS olduğunda her repository'nin işlem bilgileri ayrı tutulur. Açılır/kapanır ayrıntı alanlarında repository adı, güncel aşama ve hata bilgileri görülebilir.

Tipik durumlar:

```text
Hazırlanıyor
DeepZoom oluşturuluyor
GitHub'a yükleniyor
GitHub Pages hazırlanıyor
Web doğrulanıyor
Ana galeri güncelleniyor
Tamamlandı
Hata - yeniden denenebilir
```

### Ana galeri ayarları

Ana `galeri` repository'sinin görünen:

- başlığı
- açıklaması

aynı arayüzden değiştirilebilir. Güncelleme sırasında mevcut slayt kartlarının/sırasının korunması hedeflenir.

## HDD alanını koruma

Whole-slide DeepZoom tile klasörleri çok büyük olabilir. Bu nedenle program yerel repository'leri körlemesine silmez.

Bir `repos/gallery-XXX` klasörü silinmeden önce şu kontroller yapılır:

1. Yerel Git çalışma ağacı temiz mi?
2. Yerel HEAD ile GitHub remote commit aynı mı?
3. GitHub Pages ana sayfası erişilebilir mi?
4. `slide.dzi` web üzerinden erişilebilir mi?
5. Slayt ana galeride görünüyor mu?

Bu doğrulamalar başarılıysa yerel repository **güvenle silinebilir** olarak işaretlenir.

Arayüzde iki kullanım şekli vardır:

- yeni tamamlanan slaytlarda doğrulama sonrası otomatik temizleme
- eski `repos/gallery-*` klasörlerini tarayıp güvenli silme adaylarını kullanıcıya önerme

Ana `galeri` repository'si küçük ve sürekli güncellendiği için normalde otomatik temizleme kapsamına alınmaz.

## Kesinti ve hata sonrası devam

Her SVS için hazırlık/işlem bilgisi bir metadata dosyasında tutulur:

```text
ornek.svs.upload.json
```

Bu kayıt sayesinde:

- başlık/açıklama tekrar girilmez,
- atanmış `gallery-XXX` numarası korunur,
- yarım GitHub yüklemesi yeni repository açmadan sürdürülebilir,
- galeri güncellemesi başarısızsa sonraki çalıştırmada tekrar denenebilir.

DeepZoom üretimi geçici alanda yapılır. Üretim yarıda kesilirse eksik `slide_files/` klasörü tamamlanmış kabul edilmez.

## GitHub repository yapısı

Her slayt repository'sinde temel olarak şunlar bulunur:

```text
gallery-XXX/
├─ index.html
├─ README.md
├─ thumbnail.jpg
├─ slide.dzi
└─ slide_files/
```

Ana galeri repository'si bütün `gallery-XXX` sayfalarına bağlantı verir.

## Geliştiriciler için mimari

Kod şimdilik tek Python dosyasında tutulur; bunun amacı son kullanıcı için kurulumu ve güncellemeyi kolaylaştırmaktır. İçeride mantıksal bölümler ayrılmıştır:

- configuration / `.env`
- GitHub REST API
- Git yardımcıları
- DeepZoom üretimi
- thumbnail üretimi
- slide repository hazırlığı
- GitHub Pages doğrulaması
- ana galeri senkronizasyonu
- arşivleme ve güvenli yerel temizlik
- recovery / resume state
- Tkinter GUI

Gelecekte proje büyürse bu bölümler ayrı modüllere bölünebilir. Ancak kullanıcı tarafında yine tek giriş noktası korunması önerilir:

```bash
python whole_slide_uploader.py
```

## Katkı geliştirme fikirleri

- Daha ayrıntılı yükleme hız/ETA gösterimi
- Sürükle-bırak SVS ekleme
- Thumbnail kırpma aracı
- Repository retry kuyruğu
- GitHub API rate-limit göstergesi
- Otomatik sürüm güncelleme
- İngilizce/Türkçe arayüz seçimi
- OpenSeadragon viewer tema seçenekleri
- SVS dışındaki desteklenen WSI biçimleri
- Test suite ve CI

## Güvenlik

Gerçek `.env`, GitHub token, hasta kimliği veya kişisel/klinik tanımlayıcı bilgi repository'ye eklenmemelidir.

Whole-slide dosyasını yayınlamadan önce görüntünün paylaşım için uygun ve gerektiğinde de-identifiye edilmiş olduğundan emin olun.

## Lisans ve yeniden kullanım

Kodun başkaları tarafından yeniden kullanılmasını veya geliştirilmesini istiyorsanız repository'ye ayrıca açık bir `LICENSE` dosyası eklenmesi önerilir. Lisans seçimi proje sahibinin tercihine bırakılmıştır.

---

Bu doküman uploader'ın kullanımını ve geliştirme yönünü tek yerde tutmak için hazırlanmıştır. Davranış değiştiğinde `whole_slide_uploader.py` ile birlikte güncellenmesi önerilir.
