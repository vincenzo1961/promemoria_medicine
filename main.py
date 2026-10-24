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

# Colori disponibili per le persone: (forte, chiaro per le righe, sfondo della sveglia)
PALETTE = {
    "rosa":    ("#D6457A", "#FCE4EE", "#F9C9DC"),
    "azzurro": ("#2C6FB7", "#E1EEFB", "#BFDCF7"),
    "verde":   ("#2E9E44", "#E3F4E6", "#C5EBCB"),
    "viola":   ("#7B4FB8", "#EEE6F8", "#DDCCF2"),
    "arancio": ("#E07020", "#FDEEDF", "#FAD7B5"),
    "giallo":  ("#B38F00", "#FFF6CC", "#FFE88A"),
}

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
#  INVIO SMS
# =====================================================
def chiedi_permesso_sms():
    if not ANDROID:
        return True
    try:
        from android.permissions import Permission, check_permission, request_permissions
        if check_permission(Permission.SEND_SMS):
            return True
        request_permissions([Permission.SEND_SMS])
    except Exception as e:
        print("Permesso SMS:", e)
    return False


def invia_sms(numero, testo):
    """Invia un SMS. Restituisce (True, '') se partito, altrimenti (False, motivo)."""
    if not ANDROID:
        print(f"[SMS simulato a {numero}] {testo}")
        return True, "simulato sul PC"
    if not chiedi_permesso_sms():
        return False, "permesso SMS non concesso"
    try:
        SmsManager = autoclass("android.telephony.SmsManager")
        gestore = SmsManager.getDefault()
        parti = gestore.divideMessage(testo)
        if parti.size() > 1:
            gestore.sendMultipartTextMessage(numero, None, parti, None, None)
        else:
            gestore.sendTextMessage(numero, None, testo, None, None)
        return True, ""
    except Exception as e:
        return False, str(e)


def pulisci_numeri(testo):
    numeri = []
    for pezzo in testo.replace(";", ",").replace("\n", ",").split(","):
        n = "".join(c for c in pezzo if c.isdigit() or c == "+")
        if len(n) >= 6 and n not in numeri:
            numeri.append(n)
    return numeri


AVVISI_PREDEFINITI = {"numeri": [], "presa": True, "non_confermata": True,
                      "rimandi": True, "rimandi_max": 3}


def carica_avvisi(percorso):
    dati = dict(AVVISI_PREDEFINITI)
    if os.path.exists(percorso):
        with open(percorso, encoding="utf-8") as f:
            dati.update(json.load(f))
    return dati


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
def persone_esempio():
    return [
        {"id": "p1", "nome": "Caterina", "colore": "rosa"},
        {"id": "p2", "nome": "Tommaso", "colore": "azzurro"},
    ]


def carica_persone(percorso):
    if not os.path.exists(percorso):
        persone = persone_esempio()
        salva_medicine(percorso, persone)
        return persone
    with open(percorso, encoding="utf-8") as f:
        return json.load(f)


def colori_di(persona):
    """(forte, chiaro, sfondo sveglia) del colore scelto per la persona."""
    return PALETTE.get((persona or {}).get("colore"), PALETTE["verde"])


def medicine_esempio():
    return [
        {"id": str(uuid.uuid4()), "nome": "Pastiglia della pressione",
         "dose": "1 compressa con un bicchiere d'acqua", "orari": ["08:00", "20:00"],
         "foto": "", "audio": "", "messaggio": "", "persona": "p1"},
        {"id": str(uuid.uuid4()), "nome": "Vitamina D",
         "dose": "10 gocce", "orari": ["13:00"],
         "foto": "", "audio": "", "messaggio": "", "persona": "p2"},
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


INTESTAZIONE_REG = ["data", "ora_prevista", "ora_risposta", "medicina", "dose", "esito", "id",
                    "persona"]


def aggiorna_registro_vecchio(percorso):
    """Il registro delle versioni precedenti non aveva la colonna 'persona': la aggiunge."""
    if not os.path.exists(percorso):
        return
    with open(percorso, encoding="utf-8") as f:
        righe = list(csv.reader(f, delimiter=";"))
    if not righe or "persona" in righe[0]:
        return
    with open(percorso, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(INTESTAZIONE_REG)
        for r in righe[1:]:
            w.writerow((r + [""] * 8)[:8])


def registra(percorso, med, orario, esito, persona=""):
    nuovo = not os.path.exists(percorso)
    ora = dt.datetime.now()
    with open(percorso, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if nuovo:
            w.writerow(INTESTAZIONE_REG)
        w.writerow([ora.date().isoformat(), orario, ora.strftime("%H:%M"),
                    med["nome"], med["dose"], esito, med["id"], persona])


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
        self.persona = app.persona_di(med)
        self.forte, _, self.fondo = colori_di(self.persona)
        super().__init__(size_hint=(1, 1), auto_dismiss=False,
                         background="", background_color=hexc(self.fondo), **kw)
        self.app, self.med, self.orario, self.chiave = app, med, orario, chiave
        self.inizio = dt.datetime.now()
        self.acceso = True
        self.ev_parla = None

        box = BoxLayout(orientation="vertical", padding=dp(14), spacing=dp(10))
        sfondo(box, self.fondo)

        if self.persona:
            fascia = Label(text=self.persona["nome"].upper(), font_size=sp(40), bold=True,
                           color=(1, 1, 1, 1), size_hint_y=None, height=dp(70))
            sfondo(fascia, self.forte)
            box.add_widget(fascia)
        self.titolo = etichetta("È L'ORA DELLA MEDICINA", 28, True, ROSSO)
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
        if self.med.get("messaggio"):
            return self.med["messaggio"]
        nome = f"{self.persona['nome']}, " if self.persona else ""
        return (f"{nome}è ora di prendere {self.med['nome']}. {self.med['dose']}. "
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
        self.titolo.color = hexc(ROSSO) if self.acceso else hexc(self.fondo)

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
        if not app.medicine:
            self.lista.add_widget(etichetta("Nessuna medicina impostata", 20))
            return
        # un gruppo per ogni persona, nell'ordine dell'elenco persone; poi le medicine senza persona
        gruppi = [(p, [m for m in app.medicine if m.get("persona") == p["id"]])
                  for p in app.persone]
        ids = {p["id"] for p in app.persone}
        gruppi.append((None, [m for m in app.medicine if m.get("persona") not in ids]))
        for persona, meds in gruppi:
            if not meds:
                continue
            forte, chiaro, _ = colori_di(persona)
            if persona:
                fascia = Label(text=persona["nome"].upper(), font_size=sp(22), bold=True,
                               color=(1, 1, 1, 1), size_hint_y=None, height=dp(40))
                sfondo(fascia, forte)
                self.lista.add_widget(fascia)
            righe = sorted(((o, m) for m in meds for o in m["orari"]), key=lambda x: x[0])
            for orario, m in righe:
                self.lista.add_widget(self._riga(app, orario, m, oggi, adesso, forte, chiaro))
            self.lista.add_widget(Label(size_hint_y=None, height=dp(6)))

    def _riga(self, app, orario, m, oggi, adesso, forte, chiaro):
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
        riga = BoxLayout(size_hint_y=None, height=dp(72), spacing=dp(6), padding=(0, dp(4)))
        sfondo(riga, chiaro)
        striscia = Label(size_hint=(None, 1), width=dp(8))
        sfondo(striscia, forte)
        o = etichetta(orario, 24, True, forte)
        o.size_hint_y = 1
        o.size_hint_x = 0.25
        n = etichetta(f"{m['nome']}\n[size={int(sp(14))}]{m['dose']}[/size]", 18, halign="left")
        n.markup = True
        n.size_hint_x = 0.48
        n.bind(height=lambda i, h, riga=riga: setattr(riga, "height", max(dp(72), h + dp(8))))
        st = etichetta(stato, 14, True, colore)
        st.size_hint_y = 1
        st.size_hint_x = 0.25
        for w in (striscia, o, n, st):
            riga.add_widget(w)
        return riga


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
        box.add_widget(bottone("+ Aggiungi medicina", VERDE, 20, dp(60),
                               lambda: app.modifica(None)))
        r = BoxLayout(size_hint_y=None, height=dp(60), spacing=dp(8))
        r.add_widget(bottone("Persone", "#7B4FB8", 20, dp(60), lambda: app.vai("persone")))
        r.add_widget(bottone("Storico", BLU, 20, dp(60), lambda: app.vai("storico")))
        box.add_widget(r)
        box.add_widget(bottone("Avvisi SMS ai familiari", "#00897B", 20, dp(60),
                               lambda: app.vai("avvisi")))
        box.add_widget(bottone("Torna all'orologio", GRIGIO, 20, dp(60),
                               lambda: app.vai("principale")))
        self.add_widget(box)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self.lista.clear_widgets()
        for i, m in enumerate(app.medicine):
            persona = app.persona_di(m)
            chi = persona["nome"] if persona else "nessuna persona"
            forte = colori_di(persona)[0] if persona else "#5A5A5A"
            self.lista.add_widget(bottone(f"{m['nome']}  ({chi})\n{', '.join(m['orari'])}",
                                          forte, 18, dp(72), lambda i=i: app.modifica(i)))


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

        # --- persona: un pulsante per ogni persona ---
        box.add_widget(etichetta("Per chi è", 13, True, halign="left", altezza=dp(20)))
        self.riga_persone = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        box.add_widget(self.riga_persone)
        self.persona_id = None

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
        ids = [p["id"] for p in app.persone]
        self.persona_id = m.get("persona") if m.get("persona") in ids else (ids[0] if ids else None)
        self._mostra()

    def _disegna_persone(self):
        app = App.get_running_app()
        self.riga_persone.clear_widgets()
        if not app.persone:
            self.riga_persone.add_widget(etichetta("Aggiungi le persone dalle Impostazioni", 13,
                                                   colore=GRIGIO, altezza=dp(44)))
        for p in app.persone:
            forte, chiaro, _ = colori_di(p)
            scelta = p["id"] == self.persona_id
            b = bottone(("• " + p["nome"] + " •") if scelta else p["nome"], forte if scelta else chiaro,
                        15, dp(44), lambda pid=p["id"]: self._scegli_persona(pid))
            if not scelta:
                b.color = hexc(forte)
            self.riga_persone.add_widget(b)

    def _scegli_persona(self, pid):
        self.persona_id = pid
        self._disegna_persone()

    def _mostra(self):
        self._disegna_persone()
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
                "foto": self.foto, "audio": self.audio, "persona": self.persona_id}

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
#  IMPOSTAZIONI: PERSONE
# =====================================================
class SchermataPersone(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        sfondo(box, BG)
        box.add_widget(etichetta("Persone", 28, True))
        box.add_widget(etichetta("Tocca una persona per cambiarle nome o colore", 15,
                                 colore=GRIGIO))
        scroll = ScrollView()
        self.lista = BoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(6))
        self.lista.bind(minimum_height=self.lista.setter("height"))
        scroll.add_widget(self.lista)
        box.add_widget(scroll)
        app = App.get_running_app()
        box.add_widget(bottone("+ Aggiungi persona", VERDE, 20, dp(60),
                               lambda: app.modifica_persona(None)))
        box.add_widget(bottone("Indietro", GRIGIO, 20, dp(60), lambda: app.vai("gestione")))
        self.add_widget(box)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self.lista.clear_widgets()
        for i, p in enumerate(app.persone):
            n = sum(1 for m in app.medicine if m.get("persona") == p["id"])
            self.lista.add_widget(bottone(f"{p['nome']}\n{n} medicin{'a' if n == 1 else 'e'}",
                                          colori_di(p)[0], 20, dp(72),
                                          lambda i=i: app.modifica_persona(i)))


class SchermataPersona(Screen):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.indice = None
        self.colore = "rosa"
        box = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))
        self.fondo = sfondo(box, BG)
        self.titolo = etichetta("Persona", 26, True)
        box.add_widget(self.titolo)
        box.add_widget(etichetta("Nome", 15, halign="left", altezza=dp(22)))
        self.nome = TextInput(multiline=False, font_size=sp(22), size_hint_y=None, height=dp(52))
        box.add_widget(self.nome)
        box.add_widget(etichetta("Colore", 15, halign="left", altezza=dp(22)))
        self.griglia = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(126),
                                 spacing=dp(6))
        box.add_widget(self.griglia)
        box.add_widget(etichetta("Anteprima", 15, halign="left", altezza=dp(22)))
        self.anteprima = Label(font_size=sp(22), bold=True, color=(1, 1, 1, 1),
                               size_hint_y=None, height=dp(44))
        self.sfondo_anteprima = sfondo(self.anteprima, "#888888")
        box.add_widget(self.anteprima)
        self.riga_anteprima = Label(text="08:00   Medicina di esempio", font_size=sp(17),
                                    size_hint_y=None, height=dp(50))
        self.sfondo_riga = sfondo(self.riga_anteprima, "#FFFFFF")
        box.add_widget(self.riga_anteprima)
        box.add_widget(BoxLayout())
        app = App.get_running_app()
        box.add_widget(bottone("SALVA", VERDE, 20, dp(58), self.salva))
        r = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        r.add_widget(bottone("Elimina", ROSSO, 17, dp(52), self.elimina))
        r.add_widget(bottone("Indietro", GRIGIO, 17, dp(52), lambda: app.vai("persone")))
        box.add_widget(r)
        self.nome.bind(text=lambda *_: self._aggiorna())
        self.add_widget(box)

    def carica(self, indice):
        app = App.get_running_app()
        self.indice = indice
        if indice is None:
            usati = {p.get("colore") for p in app.persone}
            liberi = [c for c in PALETTE if c not in usati]
            self.colore = liberi[0] if liberi else "verde"
            self.nome.text = ""
            self.titolo.text = "Nuova persona"
        else:
            p = app.persone[indice]
            self.colore = p.get("colore", "verde")
            self.nome.text = p["nome"]
            self.titolo.text = "Modifica persona"
        self._aggiorna()

    def _aggiorna(self):
        self.griglia.clear_widgets()
        nomi = list(PALETTE)
        for riga in (nomi[:3], nomi[3:]):
            r = BoxLayout(spacing=dp(6))
            for c in riga:
                forte = PALETTE[c][0]
                r.add_widget(bottone(("• " + c + " •") if c == self.colore else c, forte, 17, dp(60),
                                     lambda c=c: self._scegli(c)))
            self.griglia.add_widget(r)
        forte, chiaro, _ = PALETTE[self.colore]
        self.anteprima.text = (self.nome.text.strip() or "Nome").upper()
        self.sfondo_anteprima.rgba = hexc(forte)
        self.sfondo_riga.rgba = hexc(chiaro)
        self.riga_anteprima.color = hexc(forte)

    def _scegli(self, c):
        self.colore = c
        self._aggiorna()

    def salva(self):
        nome = self.nome.text.strip()
        if not nome:
            avviso("Attenzione", "Scrivi il nome della persona.")
            return
        app = App.get_running_app()
        if self.indice is None:
            app.persone.append({"id": uuid.uuid4().hex[:8], "nome": nome, "colore": self.colore})
            self.indice = len(app.persone) - 1
        else:
            app.persone[self.indice].update({"nome": nome, "colore": self.colore})
        app.salva_persone()
        avviso("Salvato", f"«{nome}» è stata salvata.")

    def elimina(self):
        app = App.get_running_app()
        if self.indice is None:
            app.vai("persone")
            return
        p = app.persone[self.indice]
        n = sum(1 for m in app.medicine if m.get("persona") == p["id"])
        if n:
            avviso("Non si può eliminare",
                   f"{p['nome']} ha ancora {n} medicin{'a' if n == 1 else 'e'}.\n"
                   f"Prima eliminale o assegnale a un'altra persona.")
            return
        del app.persone[self.indice]
        app.salva_persone()
        app.vai("persone")


# =====================================================
#  IMPOSTAZIONI: AVVISI SMS
# =====================================================
class SchermataAvvisi(Screen):
    VOCI = [
        ("presa", "Quando una dose viene presa"),
        ("non_confermata", f"Quando una dose NON è confermata\n(nessuna risposta in {TIMEOUT_MIN} minuti)"),
        ("rimandi", "Quando una dose viene rimandata\npiù volte di seguito"),
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        esterno = BoxLayout()
        sfondo(esterno, BG)
        scroll = ScrollView()
        box = BoxLayout(orientation="vertical", size_hint_y=None, padding=dp(12), spacing=dp(6))
        box.bind(minimum_height=box.setter("height"))

        box.add_widget(etichetta("Avvisi SMS ai familiari", 24, True))
        box.add_widget(etichetta("Numeri a cui mandare gli SMS (uno per riga)", 14,
                                 halign="left", altezza=dp(22)))
        self.numeri = TextInput(font_size=sp(19), size_hint_y=None, height=dp(96),
                                input_filter=lambda t, _: "".join(
                                    c for c in t if c.isdigit() or c in "+ ,;\n"))
        box.add_widget(self.numeri)

        box.add_widget(etichetta("Manda un SMS:", 14, True, halign="left", altezza=dp(24)))
        self.interruttori = {}
        for chiave, testo in self.VOCI:
            r = BoxLayout(size_hint_y=None, height=dp(58), spacing=dp(8))
            l = etichetta(testo, 15, halign="left")
            l.size_hint_y = 1
            r.add_widget(l)
            b = bottone("", GRIGIO, 17, dp(58), lambda k=chiave: self._cambia(k))
            b.size_hint_x = None
            b.width = dp(96)
            r.add_widget(b)
            self.interruttori[chiave] = b
            box.add_widget(r)

        r = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        l = etichetta("Rinvii prima dell'avviso", 15, halign="left")
        l.size_hint_y = 1
        r.add_widget(l)
        r.add_widget(bottone("-", GRIGIO, 24, dp(52), lambda: self._rinvii(-1)))
        self.lbl_rinvii = etichetta("3", 24, True, altezza=dp(52))
        self.lbl_rinvii.size_hint_x = None
        self.lbl_rinvii.width = dp(44)
        r.add_widget(self.lbl_rinvii)
        r.add_widget(bottone("+", GRIGIO, 24, dp(52), lambda: self._rinvii(1)))
        box.add_widget(r)

        box.add_widget(etichetta("", 4, altezza=dp(6)))
        r = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        r.add_widget(bottone("SALVA", VERDE, 19, dp(56), self.salva))
        r.add_widget(bottone("SMS di prova", ARANCIO, 16, dp(56), self.prova))
        box.add_widget(r)
        box.add_widget(bottone("Indietro", GRIGIO, 17, dp(50),
                               lambda: App.get_running_app().vai("gestione")))

        box.add_widget(etichetta("Ultimi SMS", 14, True, halign="left", altezza=dp(26)))
        self.ultimi = etichetta("", 13, colore=GRIGIO, halign="left")
        box.add_widget(self.ultimi)

        scroll.add_widget(box)
        esterno.add_widget(scroll)
        self.add_widget(esterno)

    def on_pre_enter(self, *_):
        app = App.get_running_app()
        self.dati = dict(app.avvisi)
        self.numeri.text = "\n".join(self.dati["numeri"])
        self._aggiorna()

    def _aggiorna(self):
        for k, b in self.interruttori.items():
            acceso = self.dati.get(k)
            b.text = "SÌ" if acceso else "NO"
            b.background_color = hexc(VERDE if acceso else "#B0B0B0")
        self.lbl_rinvii.text = str(self.dati.get("rimandi_max", 3))
        self.ultimi.text = App.get_running_app().ultimi_sms() or "Nessun SMS inviato."

    def _cambia(self, k):
        self.dati[k] = not self.dati.get(k)
        self._aggiorna()

    def _rinvii(self, delta):
        self.dati["rimandi_max"] = max(2, min(9, self.dati.get("rimandi_max", 3) + delta))
        self._aggiorna()

    def salva(self):
        self.dati["numeri"] = pulisci_numeri(self.numeri.text)
        self.numeri.text = "\n".join(self.dati["numeri"])
        app = App.get_running_app()
        app.avvisi = dict(self.dati)
        app.salva_avvisi()
        chiedi_permesso_sms()
        n = len(self.dati["numeri"])
        avviso("Salvato", f"Avvisi salvati.\nNumeri: {n}" if n else
               "Salvato, ma non c'è nessun numero:\nnon partirà nessun SMS.")

    def prova(self):
        numeri = pulisci_numeri(self.numeri.text)
        if not numeri:
            avviso("Attenzione", "Scrivi almeno un numero di telefono.")
            return
        app = App.get_running_app()
        esiti = [app.manda_sms(n, "Promemoria Medicine: questo è un SMS di prova. "
                                  "Gli avvisi funzionano.") for n in numeri]
        self._aggiorna()
        if all(esiti):
            avviso("SMS di prova", f"SMS inviato a {len(numeri)} numer{'o' if len(numeri) == 1 else 'i'}.")
        else:
            avviso("SMS di prova", "Qualche SMS non è partito.\nControlla la SIM, il credito\n"
                                   "e il permesso «SMS» dell'app.")


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
            chi = f"[b]{r['persona']}[/b] - " if r.get("persona") else ""
            righe.append(f"{giorno}  ore {r['ora_prevista']}  {chi}{r['medicina']}\n"
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
        self.file_persone = os.path.join(cartella, "persone.json")
        self.file_avvisi = os.path.join(cartella, "avvisi.json")
        self.file_sms = os.path.join(cartella, "sms_inviati.txt")
        self.avvisi = carica_avvisi(self.file_avvisi)
        self.conta_rinvii = {}
        self.cartella_media = os.path.join(cartella, "media")
        os.makedirs(self.cartella_media, exist_ok=True)

        self.voce = Voce(cartella)
        self.persone = carica_persone(self.file_persone)
        self.medicine = carica_medicine(self.file_med)
        # medicine delle versioni precedenti, senza persona: vanno alla prima persona
        if self.persone and any("persona" not in m for m in self.medicine):
            for m in self.medicine:
                m.setdefault("persona", self.persone[0]["id"])
            salva_medicine(self.file_med, self.medicine)
        aggiorna_registro_vecchio(self.file_reg)
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
        self.sm.add_widget(SchermataAvvisi(name="avvisi"))
        self.persona_scr = SchermataPersona(name="persona")
        self.sm.add_widget(SchermataPersone(name="persone"))
        self.sm.add_widget(self.persona_scr)

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

    # ---- SMS ai familiari ----
    def salva_avvisi(self):
        salva_medicine(self.file_avvisi, self.avvisi)

    def manda_sms(self, numero, testo):
        ok, motivo = invia_sms(numero, testo)
        riga = (f"{dt.datetime.now():%d/%m %H:%M}  {numero}  "
                f"{'inviato' if ok else 'NON inviato: ' + motivo}  |  {testo}")
        try:
            with open(self.file_sms, "a", encoding="utf-8") as f:
                f.write(riga + "\n")
        except Exception:
            pass
        return ok

    def ultimi_sms(self, quanti=8):
        if not os.path.exists(self.file_sms):
            return ""
        with open(self.file_sms, encoding="utf-8") as f:
            righe = f.read().strip().splitlines()
        return "\n".join(reversed(righe[-quanti:]))

    def avvisa_familiari(self, tipo, med, orario, extra=""):
        if not self.avvisi.get(tipo) or not self.avvisi.get("numeri"):
            return
        nome = self._nome_persona(med) or "La persona"
        ora = dt.datetime.now().strftime("%H:%M")
        if tipo == "presa":
            testo = f"{nome} ha preso {med['nome']} (dose delle {orario}) alle {ora}."
        elif tipo == "non_confermata":
            testo = (f"ATTENZIONE: {nome} NON ha confermato {med['nome']} delle {orario}. "
                     f"Nessuna risposta in {TIMEOUT_MIN} minuti.")
        else:
            testo = (f"ATTENZIONE: {nome} ha rimandato {extra} volte {med['nome']} "
                     f"delle {orario} (ultimo rinvio alle {ora}).")
        testo = "Promemoria Medicine - " + testo
        for numero in self.avvisi["numeri"]:
            self.manda_sms(numero, testo)

    def salva_persone(self):
        salva_medicine(self.file_persone, self.persone)
        self.principale.aggiorna_lista()

    def modifica_persona(self, indice):
        self.persona_scr.carica(indice)
        self.sm.current = "persona"

    def persona_di(self, med):
        for p in self.persone:
            if p["id"] == med.get("persona"):
                return p
        return None

    def _nome_persona(self, med):
        p = self.persona_di(med)
        return p["nome"] if p else ""

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
        self.conta_rinvii.pop(chiave, None)
        nome = self._nome_persona(med)
        registra(self.file_reg, med, orario, esito, nome)
        self.avvisa_familiari(esito.replace(" ", "_"), med, orario)
        if esito == "presa":
            self.voce.parla(f"Grazie {nome}!" if nome else "Grazie!")
        self.principale.aggiorna_lista()
        Clock.schedule_once(lambda _: self.controlla(), 3)   # se c'è un'altra medicina in attesa

    def rimanda(self, chiave, med, orario):
        self.allarme = None
        if orario == "PROVA":
            return
        self.rimandi[chiave] = dt.datetime.now() + dt.timedelta(minutes=RIMANDA_MIN)
        registra(self.file_reg, med, orario, "rimandata", self._nome_persona(med))
        n = self.conta_rinvii.get(chiave, 0) + 1
        self.conta_rinvii[chiave] = n
        if n == self.avvisi.get("rimandi_max", 3):
            self.avvisa_familiari("rimandi", med, orario, n)
        self.voce.parla(f"Va bene, te lo ricordo tra {RIMANDA_MIN} minuti.")
        self.principale.aggiorna_lista()


if __name__ == "__main__":
    PromemoriaApp().run()
