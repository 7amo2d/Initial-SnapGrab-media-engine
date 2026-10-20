# ربط API التنزيل الخاصة بك — SnapGrab Pro 3.17.0

هذا الربط يجعل SnapGrab يجرّب API الخارجية التي تضبطها أولًا لروابط YouTube وVimeo وSoundCloud وFacebook وX/Twitter وTwitch وTikTok وIMDb. إذا انتهت المهلة، أو رجعت API خطأ، أو لم ترجع رابط تنزيل HTTPS مفهومًا وآمنًا، ينتقل الموقع إلى محرك `yt-dlp` ثم محركاته الاحتياطية. لا تُعرض مفاتيح API للمتصفح.

> **مهم:** لم يُرفق في المحادثة عنوان API أو مستنداتها؛ لذلك أُضيف adapter عام. يلزم أن تتوافق API مع إحدى صيغ الطلب والاستجابة هنا. إذا كانت API تُرجع `job_id` ثم تحتاج مسارات `status`/`polling` أو تحتاج توقيعًا مخصصًا، فلا يكفي وضع الرابط والمفتاح فقط؛ يلزم adapter خاص حسب توثيق المزود.

## 1. المطلوب من مزود API

تأكد من هذه المعلومات من لوحة API أو توثيقها:

- رابط الـendpoint الكامل، ويجب أن يستخدم `HTTPS` في الإنتاج.
- هل يستقبل `POST` بجسم JSON أم `GET` بمعاملات Query؟
- طريقة المصادقة: `Authorization: Bearer KEY` أو `x-api-key: KEY` أو غيرها.
- أسماء الحقول التي يطلبها لتحميل الرابط والجودة والصيغة.
- مثال فعلي لاستجابة نجاح بها رابط تنزيل مباشر أو قائمة خيارات.

## 2. إعداد POST الافتراضي

يُرسل SnapGrab إلى `MEDIA_PROVIDER_API_URL` طلبًا بهذا الجسم:

```json
{
  "url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "quality": "720",
  "format": "720",
  "audioFormat": "mp3"
}
```

الجودة قد تكون مثل `360` أو `720` أو `1080` أو `2160` أو `4k` أو `audio`. عند طلب صوت، تحمل `format` قيمة `audioFormat` مثل `mp3`.

تُقبل استجابة فيها رابط مباشرة، مثل:

```json
{
  "success": true,
  "title": "Video title",
  "thumbnail": "https://cdn.example.com/thumb.jpg",
  "url": "https://cdn.example.com/video.mp4",
  "filesize": 12345678,
  "type": "video/mp4"
}
```

أو قائمة صيغ:

```json
{
  "success": true,
  "data": {
    "title": "Video title",
    "thumbnail": "https://cdn.example.com/thumb.jpg",
    "formats": [
      { "url": "https://cdn.example.com/720.mp4", "quality": "720p", "filesize": 12345678, "type": "video/mp4" },
      { "url": "https://cdn.example.com/audio.mp3", "quality": "MP3", "filesize": 1234567, "type": "audio/mpeg" }
    ]
  }
}
```

تُفهم الحقول المعتادة `url`, `download_url`, `direct_url`, `file_url`, `media_url`, `video_url`, `audio_url`, `image_url`, `link`، وكذلك القوائم `options`, `formats`, `downloads`, `items`, `files`, `medias`, `results`, `photos`, `videos`. كل روابط التنزيل الناتجة يجب أن تكون HTTPS عامة؛ الروابط المحلية، وعناوين IP المباشرة، وروابط HTTP تُرفض.

## 3. متغيرات Vercel المطلوبة

افتح Vercel Dashboard → مشروع SnapGrab → **Settings → Environment Variables**، ثم أضف التالي في `Production` (وفي `Preview` إذا كنت تختبر هناك):

| المتغير | القيمة |
| --- | --- |
| `MEDIA_PROVIDER_API_URL` | رابط endpoint من مزودك، مثل `https://api.example.com/v1/download` (استبدله بعنوانك الحقيقي) |
| `MEDIA_PROVIDER_API_KEY` | المفتاح السري الخاص بك؛ اتركه فارغًا إذا كانت API بلا مصادقة |
| `MEDIA_PROVIDER_API_METHOD` | `POST` أو `GET`، والافتراضي `POST` |
| `MEDIA_PROVIDER_API_AUTH_HEADER` | `Authorization` أو مثلًا `x-api-key` |
| `MEDIA_PROVIDER_API_AUTH_SCHEME` | `Bearer` مع Authorization؛ اتركه فارغًا إذا كان رأس المصادقة يأخذ المفتاح وحده |
| `MEDIA_PROVIDER_API_TIMEOUT_MS` | مهلة الطلب بالمللي ثانية، افتراضيًا `12000` والحد الأعلى `20000` |

لا تضع المفتاح في `index.html` أو `static/` أو ملفات JavaScript التي تصل للمتصفح، ولا ترفعه إلى GitHub. تخزين المفتاح يتم في متغيرات الخادم.

### إذا كانت API تستخدم GET

اضبط `MEDIA_PROVIDER_API_METHOD=GET`. افتراضيًا تُرسل الحقول في Query باسم `url`, `quality`, `format`, `audioFormat`. غيّر الأسماء إن تطلب توثيق API ذلك:

- `MEDIA_PROVIDER_API_URL_PARAM`
- `MEDIA_PROVIDER_API_QUALITY_PARAM`
- `MEDIA_PROVIDER_API_FORMAT_PARAM`
- `MEDIA_PROVIDER_API_AUDIO_FORMAT_PARAM`

### إذا كانت API تستخدم أسماء حقول مختلفة في POST

- `MEDIA_PROVIDER_API_URL_FIELD`
- `MEDIA_PROVIDER_API_QUALITY_FIELD`
- `MEDIA_PROVIDER_API_FORMAT_FIELD`
- `MEDIA_PROVIDER_API_AUDIO_FORMAT_FIELD`
- `MEDIA_PROVIDER_API_ACTION_FIELD` (اختياري، اتركه فارغًا ما لم تطلب API حقل action)
- `MEDIA_PROVIDER_API_ACTION_VALUE` (افتراضي `download`)

### إذا كان المفتاح مطلوبًا داخل Query بدل الرأس

يمكن ضبط `MEDIA_PROVIDER_API_KEY_PARAM` باسم الحقل المطلوب، لكن يُفضّل الرأس؛ تمرير المفتاح في URL قد يجعله ظاهرًا في سجلّات المزود. لا تضبط هذا الحقل إلا إذا كانت API لا تدعم ترويسات المصادقة.

## 4. ضبط محرك yt-dlp الاحتياطي

للتأكد من أن النظام لديه fallback حقيقي عند فشل API، يجب أن يكون محرك الوسائط نفسه منشورًا ويمكن الوصول إليه. في إعدادات Vercel أضف:

| المتغير | القيمة |
| --- | --- |
| `MEDIA_ENGINE_URL` | عنوان خدمة محرك الوسائط المنشور عبر HTTPS |
| `MEDIA_ENGINE_API_KEY` | مفتاح طويل وعشوائي (24 حرفًا على الأقل) |

وفي متغيرات بيئة محرك Python نفسه، اضبط `ENGINE_API_KEY` بالقيمة نفسها. أبقِ إعدادات `MEDIA_ENGINE_URL` و`MEDIA_ENGINE_API_KEY` كما هي إذا كان محركك يعمل بالفعل.

إذا لم يكن محرك `yt-dlp` مهيأ، فالتسلسل سيكمل إلى TikWM أو Cobalt المُعدّ أو Loader.to وفق نوع الرابط وتوفّر تلك الخدمات؛ لا يُعد ذلك fallback إلى محركك المحلي.

## 5. انشر التغييرات

1. ضع ملفات الإصدار 3.17.0 في مشروعك.
2. احفظ متغيرات البيئة في Vercel.
3. نفّذ Deploy جديدًا أو Redeploy؛ تغييرات البيئة لا تسري على deployments السابقة.
4. افتح الموقع وتأكد أن لوحة المحركات تظهر `External Media API` بحالة مهيّأة.
5. جرّب رابطًا عامًا مسموحًا لك بتنزيله من كل منصة على حدة، ثم اختبر رابطًا غير مدعوم للتأكد من انتقاله إلى fallback.

لا يكفي ظهور `مهيّأ` لإثبات أن مزودك يعمل فعليًا؛ هذا يعني أن متغيرات الإعداد سليمة فقط. يجب اختبار الاستجابة الحقيقية بعد النشر.

## 6. القيود المهمة

- هذا adapter يدعم استجابة متزامنة تحتوي URL مباشرًا أو قائمة تنسيقات بها URLs. إذا كانت API تبدأ مهمة (`job_id`) ثم تحتاج polling أو ترجع معرفًا دون رابط، سيرفض الاستجابة وينتقل إلى fallback. أرسل توثيق API لإضافة دورة المهام الصحيحة.
- الـAPI يجب أن تكون مخوّلة لك، ويجب استخدام الروابط العامة والمحتوى الذي تملك الحق في تنزيله. لا يتجاوز الموقع DRM أو الجلسات الخاصة أو شروط الوصول.
- بعض روابط YouTube أو Twitch أو الشبكات الاجتماعية تتطلب جلسة، وقد يتوقف الاستخراج عند تغيرات المصدر. قائمة المواقع المدعومة في yt-dlp توضح صراحةً أن الدعم غير مضمون لكل رابط، وأن الاختبار الحي هو الطريقة الموثوقة لتحديد نجاحه: <https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md>.
- ملفات الفيديو الكبيرة لا تُنقل خلال Vercel Function بواسطة هذا adapter؛ الـAPI ترجع روابط تنزيل من مزودك إلى المتصفح مباشرةً، لتجنب حد جسم الطلب/الاستجابة البالغ 4.5MB الموثق في Vercel: <https://vercel.com/docs/functions/limitations>.
- تغييرات متغيرات البيئة تتطلب نشرًا جديدًا وفق وثائق Vercel: <https://vercel.com/docs/environment-variables/managing-environment-variables>.


## محرك Loader.to المدمج (ليس نفس API المخصصة)

المشروع يحتوي أيضًا على محرك مستقل لـLoader-compatible protocol من الكود الذي أرسلته: `api/loader.js` هو مسار الموقع، و`tools/loader_scraper.cjs` أداة CLI محلية. الواجهة تستخدم `api/loader.js` تلقائيًا بعد فشل المسارات الأعلى أولوية. لا تشغّل CLI لتنزيل ملفات كبيرة داخل Vercel Function؛ وظيفته المحلية تكتب إلى القرص وتدعم streaming، بينما مسار Vercel يعيد رابط تنزيل خارجي قصير العمر لتجنّب حدود استجابة الوظيفة.

في 3.17.0 صار المحرك يتعرف على روابط التنزيل داخل أغلفة JSON الشائعة، وحالات `failed/error/cancelled` وحالة انتهاء التحويل من دون رابط، وتقدم بصيغة `47%`. فشل التحويل النهائي يرجع الآن فورًا للواجهة بدل الانتظار حتى مهلة أربع دقائق.

**مهم:** Loader.to-compatible واجهة غير رسمية وليست API مستقرة أو مضمونة. وجود fallback لا يثبت أن مزود الخدمة متاح الآن، وقد تتغير المضيفات أو الصيغ من دون إشعار.
