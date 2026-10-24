# -*- coding: utf-8 -*-
"""
PROMEMORIA MEDICINE - versione Android (Kivy)
Pensata per un telefono/tablet dedicato, sempre in carica e con l'app aperta.

- Orologio grande + elenco delle medicine di oggi
- All'ora giusta: schermata a tutto schermo con foto, nome, dose,
  campanello + voce (sintesi vocale di Android o messaggio registrato)
- Pulsanti: L'HO PRESA / RICORDAMELO TRA 10 MINUTI / RIPETI
- Impostazioni protette: si aprono tenendo premuto il pulsante 2 secondi
- Storico delle risposte

Prova su PC:  pip install kivy pyttsx3   poi   python main.py
"""

import csv
import datetime as dt
import json
import math
import os
import shutil
import struct
import threading
import uuid
import wave

from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.core.window import Window
from kivy.graphics import Color, Rectangle
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.modalview import ModalView
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import NoTransition, Screen, ScreenManager
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import get_color_from_hex as hexc
from kivy.utils import platform

ANDROID = platform == "android"

# ---------------- impostazioni ----------------
RIMANDA_MIN = 10        # minuti del pulsante "ricordamelo dopo"
RIPETI_VOCE_SEC = 60    # ogni quanto ripete l'annuncio
TIMEOUT_MIN = 30        # dopo quanto, senza risposta, segna "non confermata"
MAX_RITARDO_ORE = 2     # dopo un riavvio non suona per orari più vecchi di così

BG = "#FFF8E7"
BG_ALLARME = "#FFE08A"
VERDE = "#2E9E44"
ARANCIO = "#E8822A"
BLU = "#2C6FB7"
ROSSO = "#B00020"
GRIGIO = "#777777"
TESTO = "#222222"

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]

EST_FOTO = (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp")
EST_AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".amr", ".3gp")


# =====================================================
#  FUNZIONI SPECIFICHE ANDROID
# =====================================================
if ANDROID:
    from jnius import autoclass
    from android.runnable import run_on_ui_thread

    PythonActivity = autoclass("org.kivy.android.PythonActivity")

    @run_on_ui_thread
    def tieni_schermo_acceso():
        # FLAG_KEEP_SCREEN_ON = 128: lo schermo non si spegne mai mentre l'app è aperta
        PythonActivity.mActivity.getWindow().addFlags(128)

    def chiedi_permessi(poi):
        """Chiede il permesso di leggere foto e audio, e SOLO DOPO apre la scelta file."""
        try:
            from android.permissions import request_permissions
            request_permissions([
                "android.permission.READ_EXTERNAL_STORAGE",
                "android.permission.READ_MEDIA_IMAGES",
                "android.permission.READ_MEDIA_AUDIO",
            ], lambda *_: Clock.schedule_once(lambda _: poi(), 0.2))
        except Exception as e:
            print("Permessi:", e)
            poi()
else:
    def tieni_schermo_acceso():
        pass

    def chiedi_permessi(poi):
        poi()


# =====================================================
#  VOCE E SUONI
# =====================================================
def crea_campanello(percorso):
    """Genera un piccolo 'din-don-dan' in formato wav (nessun file esterno necessario)."""
    if os.path.exists(percorso):
        return
    freq_campioni = 22050
    dati = bytearray()
    for f in (660, 880, 1100):
        n = int(freq_campioni * 0.35)
        for i in range(n):
            smorzamento = 1 - i / n
            v = int(20000 * smorzamento * math.sin(2 * math.pi * f * i / freq_campioni))
            dati += struct.pack("<h", v)
    with wave.open(percorso, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(freq_campioni)
        w.writeframes(bytes(dati))


class Voce:
    def __init__(self, cartella):
        self.file_campanello = os.path.join(cartella, "campanello.wav")
        crea_campanello(self.file_campanello)
        self.mp = None          # MediaPlayer Android
        self.suono = None       # SoundLoader su PC
        self.tts = None
        self.lingua_ok = False
        self.lock_pc = threading.Lock()
        if ANDROID:
            try:
                TextToSpeech = autoclass("android.speech.tts.TextToSpeech")
                self.tts = TextToSpeech(PythonActivity.mActivity, None)
            except Exception as e:
                print("TTS non disponibile:", e)

    # ---- sintesi vocale ----
    def parla(self, testo):
        if ANDROID:
            if not self.tts:
                return
            try:
                if not self.lingua_ok:
                    Locale = autoclass("java.util.Locale")
                    self.tts.setLanguage(Locale("it", "IT"))
                    self.tts.setSpeechRate(0.85)     # un po' più lenta
                    self.lingua_ok = True
                try:
                    self.tts.speak(testo, 0, None, "promemoria")   # Android 5+
                except Exception:
                    self.tts.speak(testo, 0, None)                 # telefoni più vecchi
            except Exception as e:
                print("Errore voce:", e)
        else:
            threading.Thread(target=self._parla_pc, args=(testo,), daemon=True).start()

    def _parla_pc(self, testo):
        with self.lock_pc:
            try:
                import pyttsx3
                motore = pyttsx3.init()
                motore.say(testo)
                motore.runAndWait()
            except Exception:
                print("VOCE:", testo)

    def zitto(self):
        if ANDROID and self.tts:
            try:
                self.tts.stop()
            except Exception:
                pass

    # ---- file audio ----
    def suona(self, percorso):
        self.ferma_audio()
        try:
            if ANDROID:
                MediaPlayer = autoclass("android.media.MediaPlayer")
                self.mp = MediaPlayer()
                self.mp.setDataSource(percorso)
                self.mp.prepare()
                self.mp.start()
            else:
                self.suono = SoundLoader.load(percorso)
                if self.suono:
                    self.suono.play()
        except Exception as e:
            print("Errore audio:", e)

    def ferma_audio(self):
        if self.mp:
            try:
                self.mp.release()
            except Exception:
                pass
            self.mp = None
        if self.suono:
            self.suono.stop()
            self.suono = None

    def campanello(self):
        self.suona(self.file_campanello)


# =====================================================
#  DATI
# =====================================================
def medicine_esempio():
    return [
        {"id": str(uuid.uuid4()), "nome": "Pastiglia della pressione",
         "dose": "1 compressa con un bicchiere d'acqua", "orari": ["08:00", "20:00"],
         "foto": "", "audio": "", "messaggio": ""},
        {"id": str(uuid.uuid4()), "nome": "Vitamina D",
         "dose": "10 gocce", "orari": ["13:00"],
         "foto": "", "audio": "", "messaggio": ""},
    ]


def carica_medicine(percorso):
    if not os.path.exists(percorso):
        meds = medicine_esempio()
        salva_medicine(percorso, meds)
        return meds
    with open(percorso, encoding="utf-8") as f:
        return json.load(f)


def salva_medicine(percorso, meds):
    tmp = percorso + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meds, f, ensure_ascii=False, indent=2)
    os.replace(tmp, percorso)


def registra(percorso, med, orario, esito):
    nuovo = not os.path.exists(percorso)
    ora = dt.datetime.now()
    with open(percorso, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if nuovo:
            w.writerow(["data", "ora_prevista", "ora_risposta", "medicina", "dose", "esito", "id"])
        w.writerow([ora.date().isoformat(), orario, ora.strftime("%H:%M"),
                    med["nome"], med["dose"], esito, med["id"]])


def leggi_registro(percorso):
    if not os.path.exists(percorso):
        return []
    with open(percorso, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def stato_di_oggi(percorso):
    stato = {}
    oggi = dt.date.today().isoformat()
    for r in leggi_registro(percorso):
        if r.get("data") == oggi and r.get("esito") in ("presa", "non confermata"):
            stato[(r["id"], r["ora_prevista"], oggi)] = r["esito"]
    return stato


def orario_valido(s):
    try:
        dt.datetime.strptime(s.strip(), "%H:%M")
        return True
    except ValueError:
        return False


def normalizza_orari(testo):
    parti = [p.strip().replace(".", ":") for p in testo.replace(";", ",").split(",") if p.strip()]
    if not parti or not all(orario_valido(p) for p in parti):
        return None
    return sorted({dt.datetime.strptime(p, "%H:%M").strftime("%H:%M") for p in parti})


# =====================================================
#  PICCOLI AIUTI GRAFICI
# =====================================================
def sfondo(widget, colore):
    with widget.canvas.before:
        c = Color(*hexc(colore))
        r = Rectangle(pos=widget.pos, size=widget.size)
    widget.bind(pos=lambda *_: setattr(r, "pos", widget.pos),
                size=lambda *_: setattr(r, "size", widget.size))
    return c


def etichetta(testo, size=20, bold=False, colore=TESTO, altezza=None, halign="center"):
    l = Label(text=testo, font_size=sp(size), bold=bold, color=hexc(colore),
              halign=halign, valign="middle", size_hint_y=None)
    if altezza is None:
        l.bind(width=lambda i, w: setattr(i, "text_size", (w, None)),
               texture_size=lambda i, t: setattr(i, "height", t[1] + dp(8)))
    else:
        l.height = altezza
        l.bind(size=lambda i, s: setattr(i, "text_size", s))
    return l


def bottone(testo, colore, size=22, altezza=None, azione=None):
    b = Button(text=testo, font_size=sp(size), bold=True, background_normal="",
               background_color=hexc(colore), color=(1, 1, 1, 1), halign="center",
               size_hint_y=None, height=altezza or dp(70))
    b.bind(size=lambda i, s: setattr(i, "text_size", (s[0] - dp(12), None)))
    if azione:
        b.bind(on_release=lambda *_: azione())
    return b


def avviso(titolo, testo):
    box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
    box.add_widget(etichetta(testo, 18, colore="#FFFFFF"))
    p = Popup(title=titolo, content=box, size_hint=(0.9, None), height=dp(260))
    box.add_widget(bottone("OK", BLU, azione=p.dismiss))
    p.open()


def cartelle_rapide():
    """Pulsanti per saltare subito alle cartelle dove di solito stanno foto e registrazioni."""
    if ANDROID:
        r = "/storage/emulated/0"
        candidati = [
            ("Fotocamera", [r + "/DCIM/Camera", r + "/DCIM"]),
            ("Download", [r + "/Download"]),
            ("Immagini", [r + "/Pictures"]),
            ("Registrazioni", [r + "/Recordings", r + "/Music", r + "/Voice Recorder"]),
            ("Tutto", [r]),
        ]
    else:
        h = os.path.expanduser("~")
        candidati = [
            ("Immagini", [os.path.join(h, "OneDrive", "Immagini"), os.path.join(h, "Pictures")]),
            ("Download", [os.path.join(h, "Downloads")]),
            ("Desktop", [os.path.join(h, "OneDrive", "Desktop"), os.path.join(h, "Desktop")]),
            ("Documenti", [os.path.join(h, "OneDrive", "Documenti"), os.path.join(h, "Documents")]),
        ]
    risultato = []
    for nome, percorsi in candidati:
        for p in percorsi:
            if os.path.isdir(p):
                risultato.append((nome, p))
                break
    return risultato


def scegli_file(titolo, estensioni, callback):
    chiedi_permessi(lambda: _apri_scelta_file(titolo, estensioni, callback))


def _apri_scelta_file(titolo, estensioni, callback):
    rapide = cartelle_rapide()
    inizio = rapide[0][1] if rapide else ("/storage/emulated/0" if ANDROID else os.path.expanduser("~"))
    if not os.path.isdir(inizio):
        inizio = "/"
    fc = FileChooserListView(
        path=inizio,
        filters=[lambda cartella, nome: nome.lower().endswith(estensioni)])
    box = BoxLayout(orientation="vertical", spacing=dp(6))

    # pulsanti cartelle rapide
    riga_cartelle = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(4))
    for nome, percorso in rapide:
        riga_cartelle.add_widget(bottone(nome, BLU, 14, dp(52),
                                         lambda p=percorso: setattr(fc, "path", p)))
    box.add_widget(riga_cartelle)

    # cartella attuale, così si capisce dove si è
    lbl_cartella = etichetta(inizio, 13, colore="#FFFFFF", halign="left")
    fc.bind(path=lambda _, p: setattr(lbl_cartella, "text", p))
    box.add_widget(lbl_cartella)
    box.add_widget(fc)
    riga = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(8))
    p = Popup(title=titolo, content=box, size_hint=(0.98, 0.95))

    def conferma():
        if fc.selection:
            p.dismiss()
            callback(fc.selection[0])

    riga.add_widget(bottone("Annulla", GRIGIO, azione=p.dismiss))
    riga.add_widget(bottone("Scegli", VERDE, azione=conferma))
    box.add_widget(riga)
    p.open()


class TastoLungo(Button):
    """Pulsante che agisce solo se tenuto premuto 2 secondi (protegge le impostazioni)."""

    def __init__(self, azione, **kw):
        super().__init__(**kw)
        self.azione = azione
        self._ev = None

    def on_touch_down(self, tocco):
        if self.collide_point(*tocco.pos):
            self._ev = Clock.schedule_once(lambda _: self.azione(), 2)
        return super().on_touch_down(tocco)

    def on_touch_up(self, tocco):
        if self._ev:
            self._ev.cancel()
            self._ev = None
        return super().on_touch_up(tocco)


# =====================================================
#  SCHERMATA DELLA SVEGLIA
# =====================================================
class Allarme(ModalView):
    def __init__(self, app, med, orario, chiave, **kw):
        super().__init__(size_hint=(1, 1), auto_dismiss=False,
                         background="", background_color=hexc(BG_ALLARME), **kw)
        self.app, self.med, self.orario, self.chiave = app, med, orario, chiave
        self.inizio = dt.datetime.now()
        self.acceso = True
        self.ev_parla = None

        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        sfondo(box, BG_ALLARME)

        self.titolo = etichetta("È L'ORA DELLA MEDICINA", 30, True, ROSSO)
        box.add_widget(self.titolo)
        if orario != "PROVA":
            box.add_widget(etichetta(f"ore {orario}", 24))

        foto = med.get("foto")
        if foto and os.path.exists(foto):
            box.add_widget(Image(source=foto, fit_mode="contain"))
        else:
            box.add_widget(BoxLayout())   # spazio vuoto elastico

        box.add_widget(etichetta(med["nome"], 34, True))
        box.add_widget(etichetta(med["dose"], 24))

        box.add_widget(bottone("L'HO PRESA", VERDE, 34, dp(120),
                               lambda: self.chiudi("presa")))
        riga = BoxLayout(size_hint_y=None, height=dp(90), spacing=dp(10))
        riga.add_widget(bottone(f"RICORDAMELO\nTRA {RIMANDA_MIN} MINUTI", ARANCIO, 20,
                                dp(90), self.rimanda))
        riga.add_widget(bottone("RIPETI", BLU, 22, dp(90), self.annuncia_una_volta))
        box.add_widget(riga)
        self.add_widget(box)

        self.ev_voce = Clock.schedule_interval(lambda _: self._annuncia(), RIPETI_VOCE_SEC)
        self.ev_lampeggio = Clock.schedule_interval(lambda _: self._lampeggia(), 0.7)
        Clock.schedule_once(lambda _: self._annuncia(), 0.3)

    def messaggio(self):
        return (self.med.get("messaggio") or
                f"È ora di prendere {self.med['nome']}. {self.med['dose']}. "
                f"Quando l'hai presa, premi il pulsante verde.")

    def annuncia_una_volta(self):
        audio = self.med.get("audio")
        if audio and os.path.exists(audio):
            self.app.voce.suona(audio)
        else:
            self.app.voce.parla(self.messaggio())

    def _annuncia(self):
        if dt.datetime.now() - self.inizio > dt.timedelta(minutes=TIMEOUT_MIN):
            self.chiudi("non confermata")
            return
        self.app.voce.campanello()
        self.ev_parla = Clock.schedule_once(lambda _: self.annuncia_una_volta(), 1.8)

    def _lampeggia(self):
        self.acceso = not self.acceso
        self.titolo.color = hexc(ROSSO) if self.acceso else hexc(BG_ALLARME)

    def _ferma(self):
        self.ev_voce.cancel()
        self.ev_lampeggio.cancel()
        if self.ev_parla:
            self.ev_parla.cancel()
        self.app.voce.zitto()
        self.app.voce.ferma_audio()

    def chiudi(self, esito):
        self._ferma()
        self.dismiss()
        self.app.esito(self.chiave, self.med, self.orario, esito)

    def rimanda(self):
        self._ferma()
        self.dismiss()
        self.app.rimanda(self.chiave, self.med, self.orario)


# =====================================================
#  SCHERMATA PRINCIPALE
# =====================================================
class SchermataPrincipale(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(6))
        sfondo(box, BG)
        self.lbl_ora = etichetta("", 90, True, altezza=dp(130))
        self.lbl_data = etichetta("", 24)
        box.add_widget(self.lbl_ora)
        box.add_widget(self.lbl_data)
        box.add_widget(etichetta("Medicine di oggi", 24, True))

        scroll = ScrollView()
        self.lista = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.lista.bind(minimum_height=self.lista.setter("height"))
        scroll.add_widget(self.lista)
        box.add_widget(scroll)

        box.add_widget(TastoLungo(
            text="Impostazioni (tieni premuto)", font_size=sp(13),
            size_hint_y=None, height=dp(40), background_normal="",
            background_color=hexc("#E6DCC6"), color=hexc(GRIGIO),
            azione=lambda: App.get_running_app().vai("gestione")))
        self.add_widget(box)
        self.aggiorna_ora()
        Clock.schedule_interval(lambda _: self.aggiorna_ora(), 1)

    def aggiorna_ora(self):
        ora = dt.datetime.now()
        self.lbl_ora.text = ora.strftime("%H:%M")
        self.lbl_data.text = (f"{GIORNI[ora.weekday()].capitalize()} {ora.day} "
                              f"{MESI[ora.month - 1]} {ora.year}")

    def aggiorna_lista(self):
        app = App.get_running_app()
        self.lista.clear_widgets()
        oggi = dt.date.today().isoformat()
        adesso = dt.datetime.now().strftime("%H:%M")
        righe = sorted(((o, m) for m in app.medicine for o in m["orari"]), key=lambda x: x[0])
        if not righe:
            self.lista.add_widget(etichetta("Nessuna medicina impostata", 20))
        for orario, m in righe:
            k = (m["id"], orario, oggi)
            esito = app.stato.get(k)
            if esito == "presa":
                stato, colore = "PRESA", VERDE
            elif esito == "non confermata":
                stato, colore = "NON\nCONFERMATA", ROSSO
            elif k in app.rimandi:
                stato, colore = "TRA POCO", ARANCIO
            elif orario > adesso:
                stato, colore = "da prendere", GRIGIO
            else:
                stato, colore = "", GRIGIO
            riga = BoxLayout(size_hint_y=None, height=dp(78), spacing=dp(6),
                             padding=(0, dp(4)))
            sfondo(riga, "#FFFFFF")
            o = etichetta(orario, 26, True)
            o.size_hint_y = 1
            o.size_hint_x = 0.27
            n = etichetta(f"{m['nome']}\n[size={int(sp(15))}]{m['dose']}[/size]", 19,
                          halign="left")
            n.markup = True
            n.size_hint_x = 0.48
            n.bind(height=lambda i, h, riga=riga: setattr(riga, "height", max(dp(78), h + dp(8))))
            s = etichetta(stato, 15, True, colore)
            s.size_hint_y = 1
            s.size_hint_x = 0.25
            riga.add_widget(o)
            riga.add_widget(n)
            riga.add_widget(s)
            self.lista.add_widget(riga)


# =====================================================
#  IMPOSTAZIONI: ELENCO MEDICINE
# =====================================================
class SchermataGestione(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        sfondo(box, BG)
        box.add_widget(etichetta("Impostazioni", 28, True))
        box.add_widget(etichetta("Tocca una medicina per modificarla", 16, colore=GRIGIO))
        scroll = ScrollView()
        self.lista = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.lista.bind(minimum_height=self.lista.setter("height"))
        scroll.add_widget(self.lista)
        box.add_widget(scroll)
        app = App.get_running_app()
        box.add_widget(bottone("+ Aggiungi medicina", VERDE, azione=lambda: app.modifica(None)))
        box.add_widget(bottone("Storico", BLU, azione=lambda: app.vai("storico")))
        box.add_widget(bottone("Torna all'orologio", GRIGIO, azione=lambda: app.vai("principale")))
        self.add_widget(box)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self.lista.clear_widgets()
        for i, m in enumerate(app.medicine):
            self.lista.add_widget(bottone(f"{m['nome']}\n{', '.join(m['orari'])}", "#5A5A5A",
                                          19, dp(78), lambda i=i: app.modifica(i)))


# =====================================================
#  IMPOSTAZIONI: MODIFICA DI UNA MEDICINA
# =====================================================
class SchermataModifica(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.indice = None
        self.foto = ""
        self.audio = ""

        esterno = BoxLayout()
        sfondo(esterno, BG)
        scroll = ScrollView()   # resta solo per i telefoni con schermo molto piccolo
        box = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(10), spacing=dp(5))
        box.bind(minimum_height=box.setter("height"))

        self.titolo = etichetta("Medicina", 22, True)
        box.add_widget(self.titolo)

        # --- campi di testo, compatti ---
        self.campi = {}
        for chiave, testo, alto in [
            ("nome", "Nome della medicina", False),
            ("dose", "Dose / istruzioni", False),
            ("orari", "Orari (es. 08:00, 20:00)", False),
            ("messaggio", "Frase da dire (facoltativa)", True),
        ]:
            box.add_widget(etichetta(testo, 13, halign="left", altezza=dp(18)))
            ti = TextInput(multiline=alto, font_size=sp(17), size_hint_y=None,
                           height=dp(62) if alto else dp(40), padding=(dp(8), dp(8)))
            box.add_widget(ti)
            self.campi[chiave] = ti

        # --- foto: anteprima a sinistra, pulsanti a destra ---
        box.add_widget(etichetta("Foto della medicina", 13, True, halign="left", altezza=dp(20)))
        r = BoxLayout(size_hint_y=None, height=dp(96), spacing=dp(6))
        cornice = BoxLayout(size_hint_x=0.42)
        sfondo(cornice, "#FFFFFF")
        self.anteprima = Image(fit_mode="contain")
        self.lbl_nofoto = etichetta("nessuna\nfoto", 13, colore=GRIGIO)
        self.lbl_nofoto.size_hint_y = 1
        cornice.add_widget(self.anteprima)
        self.cornice = cornice
        r.add_widget(cornice)
        col = BoxLayout(orientation="vertical", spacing=dp(6))
        col.add_widget(bottone("Scegli foto", BLU, 15, dp(45), self.scegli_foto))
        col.add_widget(bottone("Togli foto", GRIGIO, 15, dp(45), self.togli_foto))
        r.add_widget(col)
        box.add_widget(r)

        # --- messaggio vocale ---
        box.add_widget(etichetta("Messaggio vocale registrato", 13, True, halign="left",
                                 altezza=dp(20)))
        self.lbl_audio = etichetta("", 12, colore=GRIGIO, altezza=dp(18))
        box.add_widget(self.lbl_audio)
        r = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        r.add_widget(bottone("Scegli", BLU, 15, dp(44), self.scegli_audio))
        r.add_widget(bottone("Ascolta", BLU, 15, dp(44), self.ascolta))
        r.add_widget(bottone("Togli", GRIGIO, 15, dp(44), self.togli_audio))
        box.add_widget(r)

        # --- azioni: due per riga ---
        box.add_widget(etichetta("", 4, altezza=dp(4)))
        r = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        r.add_widget(bottone("SALVA", VERDE, 18, dp(52), self.salva))
        r.add_widget(bottone("Prova la sveglia", ARANCIO, 15, dp(52), self.prova))
        box.add_widget(r)
        r = BoxLayout(size_hint_y=None, height=dp(46), spacing=dp(6))
        r.add_widget(bottone("Elimina", ROSSO, 15, dp(46), self.elimina))
        r.add_widget(bottone("Indietro", GRIGIO, 15, dp(46),
                             lambda: App.get_running_app().vai("gestione")))
        box.add_widget(r)

        scroll.add_widget(box)
        esterno.add_widget(scroll)
        self.add_widget(esterno)

    # ---- caricamento ----
    def carica(self, indice):
        app = App.get_running_app()
        self.indice = indice
        m = app.medicine[indice] if indice is not None else {}
        self.titolo.text = "Modifica medicina" if indice is not None else "Nuova medicina"
        self.campi["nome"].text = m.get("nome", "")
        self.campi["dose"].text = m.get("dose", "")
        self.campi["orari"].text = ", ".join(m.get("orari", []))
        self.campi["messaggio"].text = m.get("messaggio", "")
        self.foto = m.get("foto", "")
        self.audio = m.get("audio", "")
        self._mostra()

    def _mostra(self):
        ce_foto = bool(self.foto and os.path.exists(self.foto))
        self.cornice.clear_widgets()
        if ce_foto:
            self.anteprima.source = self.foto
            self.cornice.add_widget(self.anteprima)
        else:
            self.anteprima.source = ""
            self.cornice.add_widget(self.lbl_nofoto)
        self.lbl_audio.text = ("Messaggio vocale impostato" if self.audio
                               else "Nessuno: userà la voce del telefono")

    # ---- foto e audio ----
    def scegli_foto(self):
        scegli_file("Scegli la foto", EST_FOTO, self._foto_scelta)

    def _foto_scelta(self, percorso):
        self.foto = App.get_running_app().copia_in_app(percorso)
        self._mostra()

    def togli_foto(self):
        self.foto = ""
        self._mostra()

    def scegli_audio(self):
        scegli_file("Scegli il messaggio vocale", EST_AUDIO, self._audio_scelto)

    def _audio_scelto(self, percorso):
        self.audio = App.get_running_app().copia_in_app(percorso)
        self._mostra()

    def togli_audio(self):
        self.audio = ""
        self._mostra()

    def ascolta(self):
        app = App.get_running_app()
        if self.audio and os.path.exists(self.audio):
            app.voce.suona(self.audio)
        else:
            nome = self.campi["nome"].text.strip() or "la medicina"
            app.voce.parla(self.campi["messaggio"].text.strip() or f"È ora di prendere {nome}.")

    # ---- salvataggio ----
    def _dati(self):
        nome = self.campi["nome"].text.strip()
        if not nome:
            avviso("Attenzione", "Scrivi il nome della medicina.")
            return None
        orari = normalizza_orari(self.campi["orari"].text)
        if not orari:
            avviso("Attenzione", "Orari non validi.\nScrivi per esempio: 08:00, 20:30")
            return None
        return {"nome": nome, "dose": self.campi["dose"].text.strip(), "orari": orari,
                "messaggio": self.campi["messaggio"].text.strip(),
                "foto": self.foto, "audio": self.audio}

    def salva(self):
        dati = self._dati()
        if not dati:
            return
        app = App.get_running_app()
        if self.indice is None:
            dati["id"] = str(uuid.uuid4())
            app.medicine.append(dati)
            self.indice = len(app.medicine) - 1
        else:
            app.medicine[self.indice].update(dati)
        app.salva()
        avviso("Salvato", f"«{dati['nome']}» è stata salvata.")

    def prova(self):
        dati = self._dati()
        if not dati:
            return
        dati["id"] = "prova"
        app = App.get_running_app()
        app.apri_allarme(dati, "PROVA", ("prova", "PROVA", "prova"))

    def elimina(self):
        if self.indice is None:
            App.get_running_app().vai("gestione")
            return
        app = App.get_running_app()
        nome = app.medicine[self.indice]["nome"]
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        box.add_widget(etichetta(f"Eliminare «{nome}»?", 20, colore="#FFFFFF"))
        p = Popup(title="Elimina", content=box, size_hint=(0.9, None), height=dp(250))

        def si():
            p.dismiss()
            del app.medicine[self.indice]
            app.salva()
            app.vai("gestione")

        riga = BoxLayout(size_hint_y=None, height=dp(64), spacing=dp(8))
        riga.add_widget(bottone("No", GRIGIO, azione=p.dismiss))
        riga.add_widget(bottone("Sì, elimina", ROSSO, azione=si))
        box.add_widget(riga)
        p.open()


# =====================================================
#  IMPOSTAZIONI: STORICO
# =====================================================
class SchermataStorico(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        sfondo(box, BG)
        box.add_widget(etichetta("Storico (ultimi 100)", 26, True))
        scroll = ScrollView()
        self.testo = etichetta("", 17, halign="left")
        self.testo.markup = True
        scroll.add_widget(self.testo)
        box.add_widget(scroll)
        box.add_widget(bottone("Indietro", GRIGIO,
                               azione=lambda: App.get_running_app().vai("gestione")))
        self.add_widget(box)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        colori = {"presa": VERDE, "rimandata": ARANCIO, "non confermata": ROSSO}
        righe = []
        for r in reversed(leggi_registro(app.file_reg)[-100:]):
            try:
                giorno = dt.date.fromisoformat(r["data"]).strftime("%d/%m")
            except ValueError:
                giorno = r["data"]
            col = colori.get(r["esito"], GRIGIO).lstrip("#")
            righe.append(f"{giorno}  ore {r['ora_prevista']}  {r['medicina']}\n"
                         f"      [color={col}][b]{r['esito']}[/b][/color] alle {r['ora_risposta']}")
        self.testo.text = "\n".join(righe) if righe else "Nessuna registrazione."


# =====================================================
#  APPLICAZIONE
# =====================================================
class PromemoriaApp(App):
    title = "Promemoria Medicine"

    def build(self):
        Window.clearcolor = hexc(BG)
        if not ANDROID:
            Window.size = (420, 860)

        cartella = self.user_data_dir
        self.file_med = os.path.join(cartella, "medicine.json")
        self.file_reg = os.path.join(cartella, "registro.csv")
        self.cartella_media = os.path.join(cartella, "media")
        os.makedirs(self.cartella_media, exist_ok=True)

        self.voce = Voce(cartella)
        self.medicine = carica_medicine(self.file_med)
        self.stato = stato_di_oggi(self.file_reg)
        self.rimandi = {}
        self.allarme = None

        self.sm = ScreenManager(transition=NoTransition())
        self.principale = SchermataPrincipale(name="principale")
        self.modifica_scr = SchermataModifica(name="modifica")
        self.sm.add_widget(self.principale)
        self.sm.add_widget(SchermataGestione(name="gestione"))
        self.sm.add_widget(self.modifica_scr)
        self.sm.add_widget(SchermataStorico(name="storico"))

        self.principale.aggiorna_lista()
        Clock.schedule_once(lambda _: self.controlla(), 2)
        Clock.schedule_interval(lambda _: self.controlla(), 15)
        tieni_schermo_acceso()
        return self.sm

    def on_pause(self):
        return True

    def on_resume(self):
        tieni_schermo_acceso()

    # ---- navigazione ----
    def vai(self, nome):
        if nome == "principale":
            self.principale.aggiorna_lista()
        self.sm.current = nome

    def modifica(self, indice):
        self.modifica_scr.carica(indice)
        self.sm.current = "modifica"

    def salva(self):
        salva_medicine(self.file_med, self.medicine)
        self.principale.aggiorna_lista()

    def copia_in_app(self, percorso):
        """Copia foto/audio dentro l'app, così restano anche se l'originale viene cancellato."""
        est = os.path.splitext(percorso)[1].lower()
        if est == ".jfif":          # è un normale jpg salvato dal browser
            est = ".jpg"
        dest = os.path.join(self.cartella_media, uuid.uuid4().hex + est)
        try:
            shutil.copy(percorso, dest)
            return dest
        except Exception as e:
            avviso("Errore", f"Non riesco a leggere il file:\n{e}")
            return ""

    # ---- controllo orari ----
    def controlla(self):
        if self.allarme is None:
            adesso = dt.datetime.now()
            oggi = adesso.date().isoformat()
            for m in self.medicine:
                for orario in m["orari"]:
                    k = (m["id"], orario, oggi)
                    if self.stato.get(k) in ("presa", "non confermata"):
                        continue
                    h, mi = map(int, orario.split(":"))
                    previsto = adesso.replace(hour=h, minute=mi, second=0, microsecond=0)
                    if adesso < previsto:
                        continue
                    if k in self.rimandi:
                        if adesso < self.rimandi[k]:
                            continue
                    elif adesso - previsto > dt.timedelta(hours=MAX_RITARDO_ORE):
                        continue
                    self.apri_allarme(m, orario, k)
                    break
                if self.allarme:
                    break
        if self.sm.current == "principale":
            self.principale.aggiorna_lista()

    def apri_allarme(self, med, orario, chiave):
        if self.allarme:
            return
        self.allarme = Allarme(self, med, orario, chiave)
        self.allarme.open()

    # ---- risposte ----
    def esito(self, chiave, med, orario, esito):
        self.allarme = None
        self.rimandi.pop(chiave, None)
        if orario == "PROVA":
            if esito == "presa":
                self.voce.parla("Prova riuscita.")
            return
        self.stato[chiave] = esito
        registra(self.file_reg, med, orario, esito)
        if esito == "presa":
            self.voce.parla("Bravo, grazie!")
        self.principale.aggiorna_lista()
        Clock.schedule_once(lambda _: self.controlla(), 3)   # se c'è un'altra medicina in attesa

    def rimanda(self, chiave, med, orario):
        self.allarme = None
        if orario == "PROVA":
            return
        self.rimandi[chiave] = dt.datetime.now() + dt.timedelta(minutes=RIMANDA_MIN)
        registra(self.file_reg, med, orario, "rimandata")
        self.voce.parla(f"Va bene, te lo ricordo tra {RIMANDA_MIN} minuti.")
        self.principale.aggiorna_lista()


if __name__ == "__main__":
    PromemoriaApp().run()
