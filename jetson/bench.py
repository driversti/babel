"""Throughput of bge-m3 on this machine, at several token caps.

Run inside the container, on the Jetson. Prints a table; the numbers go into
the design spec's "Still unmeasured" section, which is what closes it.

Deliberately measures with *real* article text rather than lorem ipsum: token
count per character varies by more than 2x across the scripts in this archive
(Latin against Persian and Cyrillic), and a benchmark on English alone would
report a throughput the corpus never sees.
"""

import argparse
import os
import statistics
import time

import torch
from transformers import AutoModel, AutoTokenizer

# One representative body per script family in the archive, trimmed to exactly
# the corpus p90 of 4,600 characters. Pulled 2026-07-31 from the live archive
# (erepublik@<deploy-host>) with:
#   SELECT id, left(body, 4600) FROM articles
#   WHERE length(body) BETWEEN 4600 AND 6000 AND body ~ '<script range>'
#   ORDER BY id DESC LIMIT 1
# using '[؀-ۿ]' for Persian and '[Ѐ-ӿ]' for Cyrillic, and excluding both
# ranges for Latin. Article ids: latin=2796545 (Polish), cyrillic=2794482
# (Bulgarian), persian=2796355 (Persian, with some embedded English proper
# nouns — representative of the corpus, not scrubbed for purity).
SAMPLES = {
    "latin": """Sama informacja o dziennym zysku nie wystarczy, żeby ocenić, czy dana firma się opłaca. Równie ważne jest to, ile kosztuje jej zakup i po jakim czasie inwestycja się zwróci.

Do przeliczeń przyjąłem kurs z Monetary Market: około 1803 PLN za 1 GOLD.

Koszt pełnej fabryki:

Q1: około 18 030 PLN

Q2: około 54 090 PLN

Q3: około 144 240 PLN

Q4: około 324 540 PLN

Q5: około 685 140 PLN

Q6: około 1 406 339 PLN

Q7: około 2 217 689 PLN

Dla Raw Companies ceny wyglądają inaczej:

Q1 Raw: 1500 PLN

Q2 Raw: 3000 PLN

Q3 Raw: około 18 030 PLN

Q4 Raw: 8500 PLN

Q5 Raw: około 63 105 PLN

To ma duże znaczenie. Przykładowo Q4 Raw kosztuje bardzo niewiele w porównaniu z tym, ile potrafi zarobić, dlatego często zwraca się szybciej niż Q5, mimo że Q5 generuje większy zysk każdego dnia.

Jak liczę czas zwrotu?

Wzór jest prosty:

czas zwrotu = koszt firmy / dzienny zysk

Jeżeli firma przynosi stratę, nie ma sensu liczyć czasu zwrotu.

Food

Food jest dość stabilną branżą, ale nie każda jakość jest równie opłacalna.

Food Q1 kosztuje około 18 030 PLN, zarabia około 23 PLN dziennie i zwraca się po około 770 dniach.

Food Q2 kosztuje około 54 090 PLN, zarabia około 100 PLN dziennie i zwraca się po około 542 dniach.

Food Q3 kosztuje około 144 240 PLN, zarabia około 180 PLN dziennie i zwraca się po około 801 dniach.

Food Q4 kosztuje około 324 540 PLN, zarabia około 328 PLN dziennie i zwraca się po około 991 dniach.

Food Q5 kosztuje około 685 140 PLN, zarabia około 360 PLN dziennie i zwraca się po około 1902 dniach.

Food Q6 kosztuje około 1 406 339 PLN, zarabia około 559 PLN dziennie i zwraca się po około 2514 dniach.

Food Q7 kosztuje około 2 217 689 PLN, zarabia około 172 PLN dziennie i zwraca się dopiero po około 12 887 dniach.

Najlepiej wypada obecnie Food Q2. Co prawda Food Q6 przynosi większy dzienny zysk, ale wysoki koszt zakupu sprawia, że inwestycja zwraca się znacznie wolniej.

Weapon

W przypadku Weapon opłacalne są tylko wybrane poziomy jakości.

Weapon Q1, Q2 i Q3 są obecnie stratne.

Weapon Q4 zarabia około 18 PLN dziennie. Przy koszcie około 324 540 PLN oznacza to zwrot dopiero po około 18 453 dniach.

Weapon Q5 kosztuje około 685 140 PLN, zarabia około 1374 PLN dziennie i zwraca się po około 499 dniach.

Weapon Q6 kosztuje około 1 406 339 PLN, zarabia około 909 PLN dziennie i zwraca się po około 1548 dniach.

Weapon Q7 kosztuje około 2 217 689 PLN, zarabia około 50 PLN dziennie, więc zwrot następuje dopiero po około 44 439 dniach.

Najlepiej wygląda Weapon Q5. Warto jednak pamiętać, że wysoka opłacalność na papierze nie zawsze oznacza łatwą sprzedaż. Rynek Q5 jest o wiele mniej płynny niż Q7.

Food Raw Material

Tutaj wyniki są naprawdę ciekawe.

Food Raw Q1 i Q2 są stratne.

Food Raw Q3 kosztuje około 18 030 PLN, zarabia około 39 PLN dziennie i zwraca się po około 458 dniach.

Food Raw Q4 kosztuje zaledwie 8500 PLN, zarabia około 87 PLN dziennie i zwraca się po około 97 dniach.

Food Raw Q5 kosztuje około 63 105 PLN, zarabia około 159 PLN dziennie i zwraca się po około 396 dniach.

Jeśli patrzeć wyłącznie na zwrot z inwestycji, Food Raw Q4 jest zdecydowanym zwycięzcą. Q5 daje wyższy dzienny zysk, ale na odzyskanie zainwestowanych pieniędzy trzeba czekać znacznie dłużej.

Weapon Raw Material

Sytuacja wygląda bardzo podobnie.

Weapon Raw Q1 i Q2 są stratne.

Weapon Raw Q3 kosztuje około 18 030 PLN, zarabia około 21 PLN dziennie i zwraca się po około 873 dniach.

Weapon Raw Q4 kosztuje 8500 PLN, zarabia około 61 PLN dziennie i zwraca się po około 139 dniach.

Weapon Raw Q5 kosztuje około 63 105 PLN, zarabia około 122 PLN dziennie i zwraca się po około 518 dniach.

Również tutaj najlepszy stosunek kosztu do zysku ma Weapon Raw Q4.

Aircraft

Aircraft wymaga pracowników, dlatego zakładam pełne wykorzystanie wszystkich slotów.

Aircraft Q1–Q4 są stratne.

Aircraft Q5 kosztuje około 685 140 PLN. Przy pełnym obłożeniu pracownikami zarabia około 1583 PLN dziennie i zwraca się po około 433 dniach.

Aircraft Q5 wygląda całkiem dobrze, ale tylko wtedy, gdy masz zapewnionych pracowników lub Work Tickets i rynek utrzyma obecne ceny. Jest to zdecydowanie bardziej ryzykowna inwestycja niż Food Raw czy Weapon Raw.

House

Obecnie produkcja House jest po prostu nieopłacalna.

House Q1 traci około 2018 PLN dziennie.

House Q2 traci około 2282 PLN dziennie.

House Q3 traci około 2858 PLN dziennie.

House Q4 traci około 5013 PLN dziennie.

House Q5 traci około 35 114 PLN dziennie.

Przy obecnych cenach i kosztach pracy trudno znaleźć argument za inwestowaniem w House.

Aircraft Raw i House Raw

Obie branże są obecnie nieopłacalne.

Ai""",
    "cyrillic": """В условията на игра, в която очакванията често изпреварват реалността, смятам за по-честно да започна с яснота: кандидатурата ми за президент не идва с големи обещания, нито с готови сценарии за успех. Не заявявам, че мандатът ще бъде безупречен или дори че ще бъде изкаран до край. Не представям кабинет, не демонстрирам подробна програма и не претендирам за универсални решения. Вместо това предлагам прост, изпълним фокус: повече битки, повече възможности за медали и координирана игра със съюзниците, когато това е възможно и рационално.

Подобна позиция може да изглежда необичайна в контекст, в който политическите послания често се градят върху амбициозни обещания. Но именно липсата на свръхамбиции може да се окаже предимство. В динамична среда като тази на играта устойчивостта не се постига чрез грандиозни планове, а чрез последователни, изпълними действия. Целта на този подход е да намали разминаването между заявено и постигнато, като постави акцент върху конкретни игрови резултати, които са измерими и достъпни за широк кръг играчи.

Прагматичен фокус върху битките

Основната оперативна рамка на мандата ще бъде ориентирана към максимизиране на възможностите за участие в битки. Това включва избор на фронтове, при които вероятността за активни, чести и смислени сражения е най-висока. В практиката това означава приоритизиране на ситуации, в които играчите могат да натрупват опит, да печелят медали и да поддържат активност без излишно разпиляване на ресурси.

Вместо сложни кампании с висока степен на неопределеност, фокусът ще бъде върху стабилен ритъм на действията: навременни включвания, ясно разпределение на усилията и координация, която улеснява масовото участие. Този подход не изисква специална организационна инфраструктура, а разчита на дисциплина и прозрачност в решенията. Когато условията са благоприятни, ще се търсят възможности за съвместни операции със съюзници, с оглед синхронизиране на ударите и оптимизиране на резултатите.

Координация със съюзници без свръхочаквания

Сътрудничеството със съюзници ще бъде инструмент, а не самоцел. Там, където съществува реална оперативна полза — повече активни битки, по-добро разпределение на усилията, по-висока ефективност — ще се търси синхрон. Там, където координацията изисква непропорционални усилия или води до стагнация, приоритет ще има активната игра на собствените ни участници.

Важно е да се подчертае, че този модел не предполага зависимост от външни фактори. Той е изграден така, че да функционира и при ограничена подкрепа, и при променящи се обстоятелства. Ключовият принцип е адаптивност: решенията се вземат според текущата конфигурация на картата, наличните ресурси и реалната ангажираност на играчите.

Липса на кабинет като управленски избор

Отсъствието на предварително обявен кабинет не е дефицит, а съзнателен избор. В среда с висока динамика фиксираните структури често изостават от реалността. По-гъвкавият модел позволява включване на активни участници според моментните задачи, без формални ограничения. Това създава възможност за по-широко участие и намалява риска от административна инерция.

При необходимост ще се търси експертност по конкретни теми — оперативно планиране, комуникация, логистика — но без предварително обвързване с позиции и роли. Така се запазва оперативната свобода и се избягват излишни очаквания, които не носят пряка полза за игровите резултати.

Без големи проекти и без реторика за „завръщане“

Тази кандидатура не се опира на обещания за „голямо възраждане“ или структурни реформи. Подобни наративи често изискват време, ресурс и консенсус, които не са гарантирани. Вместо това се предлага ограничен, но ясен обхват на действие: поддържане на активност, осигуряване на възможности за медали и ефективно използване на наличните прозорци за битки.

Този минималистичен подход има предимството на предвидимостта. Играчите знаят какво да очакват: редовни решения, насочени към конкретни битки, и прозрачни мотиви зад тях. Липсата на мащабни обещания намалява риска от разочарование и създава пространство за реални, измерими резултати.

Управление чрез прозрачност и адаптация

В рамките на мандата решенията ще бъдат обяснявани с оперативна логика: защо се избира даден фронт, каква е очакваната полза, какви са рисковете. Когато условията се променят, курсът ще се коригира без излишна реторика. Този модел не разчита на харизма или символика, а на последователност и яснота.

Възможно е да има периоди на по-ниска активност или непредвидени затруднения. Това не е изключение, а част от игровата среда. В такива моменти приоритет ще бъде запазването на функцио""",
    "persian": """در تاریخ سیاسی eIran، نام بعضی احزاب تنها به یک دوره کوتاه یا چند انتخابات محدود نمی‌شود. این نام‌ها با خاطرات نسل‌های مختلف بازیکنان، رقابت‌های سیاسی، انتخابات ریاست‌جمهوری و فراز و نشیب‌های جامعه ایران در eRepublik پیوند خورده‌اند. Iran Green Party یا حزب سبز ایران بدون تردید یکی از همین احزاب تاریخی است؛ حزبی که فعالیت خود را در سال‌های ابتدایی شکل‌گیری eIran آغاز کرد و با وجود تغییر نام و ساختار، توانست برای سال‌ها در فضای سیاسی کشور باقی بماند.

آغاز فعالیت در سال ۲۰۰۸

نخستین نسخه حزب سبز ایران در ژوئن ۲۰۰۸ تأسیس شد؛ زمانی که ساختار سیاسی eIran هنوز در حال شکل‌گیری بود و بسیاری از احزاب بزرگ و شناخته‌شده سال‌های بعد وجود نداشتند.

حزب سبز خیلی زود توانست به یکی از احزاب مهم کشور تبدیل شود. تعداد اعضای آن در دوره اوج به حدود صد نفر رسید؛ رقمی قابل توجه برای جامعه آن روز eIran. حضور فعال اعضای حزب در انتخابات، دولت‌ها، کنگره و فعالیت‌های اجتماعی باعث شد نام Green Party در کنار احزاب قدرتمند و باسابقه ایران قرار گیرد.

نخستین رئیس شناخته‌شده حزب Van HeIsing بود که از ژوئن تا نوامبر ۲۰۰۸ رهبری حزب را بر عهده داشت. پس از او بازیکنانی مانند zfarhad2000، alireza-irani، Lord of Alamut، Elmira، Boriani و شماری دیگر در دوره‌های مختلف به ریاست حزب رسیدند.

اصول و آرمان‌های حزب

هویت حزب سبز تنها به رنگ یا نام آن محدود نبود. در معرفی تاریخی حزب، چند مفهوم اصلی به‌عنوان پایه‌های فکری آن مطرح شده بود:

جنبش سبز: حمایت از فعالیت‌های فرهنگی، علمی و سازنده.

اندیشه سبز: استقبال از ایده‌های تازه و راهکارهای جدید.

سرزمین سبز: دفاع از تمامیت ارضی و مرزهای eIran.

گفت‌وگوی سبز: تأکید بر گفت‌وگوی محترمانه و حل اختلافات در فضایی دوستانه.

امید سبز: حفظ امید به موفقیت ایران در دنیای مجازی.

اقتصاد سبز: مبارزه با فقر اقتصادی، اجتماعی، سیاسی، علمی و فرهنگی.

پرچم سبز: نمادی از هویت، افتخار، استقلال، دوستی و صلح.

انتخاب سبز: تشویق شهروندان به مشارکت در دموکراسی و فعالیت‌های سیاسی eRepublik.

این اصول نشان می‌داد که حزب سبز خود را تنها یک تشکل انتخاباتی نمی‌دانست، بلکه تلاش داشت جامعه‌ای فعال، آگاه و متحد در eIran ایجاد کند.

دوران قدرت و ریاست‌جمهوری

در سال‌های نخست فعالیت، حزب سبز چندین بار موفق شد نامزدهای خود را به مقام ریاست‌جمهوری eIran برساند. این موفقیت‌ها حزب را از یک تشکل معمولی به یکی از نیروهای اصلی سیاست کشور تبدیل کرد.

از جمله رؤسای‌جمهور مرتبط با حزب سبز می‌توان به atilaa، alireza-irani، Lord of Alamut، agha rahman و Laya اشاره کرد. برخی از این شهروندان بیش از یک بار با حمایت یا عضویت در حزب سبز به ریاست‌جمهوری رسیدند.

دولت‌های وابسته به حزب سبز در دوره‌هایی فعالیت می‌کردند که eIran با جنگ‌ها، مشکلات داخلی، کاهش جمعیت فعال و رقابت شدید میان احزاب روبه‌رو بود. حضور چندباره اعضای حزب در بالاترین مقام سیاسی کشور، میزان نفوذ و اعتماد جامعه به این جریان را نشان می‌دهد.

یکی از چهره‌های مهم این دوران Laya بود. او در دوره‌ای که چند دولت کم‌تحرک در کشور فعالیت کرده بودند، ساختار وزارت دفاع را بازسازی کرد و تلاش نمود حضور نظامی ایران در نبردهای متحدان دوباره جدی گرفته شود.

پایان دوره نخست و تولد خانواده سبز

در ۱۶ ژوئیه ۲۰۱۳، فعالیت نسخه نخست Iran Green Party پایان یافت و تشکل جدیدی با نام Iran Green Family جایگزین آن شد.

خانواده سبز بسیاری از ایده‌ها و اعضای حزب قدیمی را حفظ کرد، اما ساختار و هویت تازه‌ای به خود گرفت. این حزب در آغاز گرایش راست میانه داشت و در سال ۲۰۱۴ گرایش رسمی خود را به میانه تغییر داد.

نخستین رئیس Iran Green Family بازیکن AMIRKABIR72 بود. پس از او شهروندانی مانند Darth Cholghad و little baby ریاست حزب را بر عهده گرفتند. خانواده سبز در سال‌های بعد نیز در انتخابات حزبی و کنگره حضور داشت و توانست بخشی از میراث سیاسی Green Party را زنده نگه دارد.

با این حال، Iran Green Family هرگز به اندازه نسخه نخست حزب سبز قدرتمند نشد. کاهش جمعیت فعال بازی، تغییر نسل بازیکنان و تحولات سیاسی eIran از عواملی بودند که بر میزان فعالیت این حزب تأثیر گذاشتند.

بازگشت نام تاریخی در سال ۲۰۱۹

در فوریه ۲۰۱۹، پس از نزدیک به شش سال فعالیت با نام Iran Green Family، عنوان تاریخی Iran Green Party دوباره احیا شد.

بازگشت این نام تنها یک تغییر ظاهری نبود؛ بلکه تلاشی برای زنده‌کردن یکی از قدیمی‌ترین برندهای سیاسی eIran محسوب می‌شد. حزب تازه‌احیاشده فعالیت خود را به‌عنوان ششمین حزب بزرگ ایران آغاز کرد و گرایش میانه و ایدئولوژی آزادی‌خواهانه را برای خود برگزید.

از آن زمان تاکنون، حزب سبز دوره‌های متفاوتی از فعالیت، رکود و بازسازی را تجربه کرده است. تعداد اعضا و قدرت انتخاباتی آن مانند سال‌های طلایی گذشته ثابت نبوده، اما نام حزب همچنان بخشی از تاریخ سیاسی eIran باقی مانده است.

میراث حزب سبز

اهمیت حزب سبز را نباید تنها با تعداد کرسی‌های کنگره یا پیروزی‌های انتخاباتی سنجید. این حزب یکی از نخستین ساختارهای سیاسی منظم eIran بود و بسیاری از بازیکنان قدیمی فعالیت سیاسی خود را در آن آغاز کردند.

حزب سبز در طول تاریخ خود سه دوره اصلی را پشت سر گذاشته است:

* دوره نخست Iran Green Party از سال ۲۰۰۸ تا ۲۰۱۳

* دوره Iran Green Family از سال ۲۰۱۳ ت""",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=os.environ.get("MODEL_DIR", "/models/bge-m3"))
    ap.add_argument("--batches", type=int, default=10)
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(args.model_dir)
    model = AutoModel.from_pretrained(args.model_dir, torch_dtype=torch.float16)
    model = model.to("cuda").eval()

    print(f"{'tokens':>7} {'batch':>6} {'script':>9} {'docs/s':>8} {'ms/batch':>9}")
    for max_tokens in (512, 1024, 2048):
        for batch_size in (8, 16, 32):
            for name, text in SAMPLES.items():
                texts = [text] * batch_size
                timings = []
                for i in range(args.batches + 2):
                    torch.cuda.synchronize()
                    start = time.perf_counter()
                    with torch.inference_mode():
                        enc = tok(texts, padding=True, truncation=True,
                                  max_length=max_tokens, return_tensors="pt").to("cuda")
                        out = model(**enc).last_hidden_state[:, 0]
                        torch.nn.functional.normalize(out, p=2, dim=-1)
                    torch.cuda.synchronize()
                    # The first two iterations pay for CUDA context setup and
                    # kernel autotuning, which no production batch pays again.
                    if i >= 2:
                        timings.append(time.perf_counter() - start)
                per_batch = statistics.median(timings)
                print(f"{max_tokens:>7} {batch_size:>6} {name:>9} "
                      f"{batch_size / per_batch:>8.1f} {per_batch * 1000:>9.0f}")


if __name__ == "__main__":
    main()
